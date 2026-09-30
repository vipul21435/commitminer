# CommitMiner plan

CommitMiner mines and ranks candidate fail-to-pass tasks from real repository history, so a
task author only spends time on commits that can become good tasks. A good candidate is a
commit (or merged pull request) that changes source and tests together, is small and focused,
and whose new or changed tests are likely to fail on the parent commit and pass on the fix.
CommitMiner proposes and ranks; it does not build environments or run the flip itself.
Its output (JSONL) is the input for downstream environment builders.

## Decisions

- Fresh repository, not a fork. PyDriller (Apache-2.0) was considered: it is a general
  git-analysis framework built on GitPython, and CommitMiner needs only a narrow, well-typed
  parse of `git log`. Calling the git CLI through `subprocess` keeps the dependency set small
  and `mypy --strict` clean. No other small, permissively licensed task-mining project was
  found (`gh search repos` for fail-to-pass and task-mining terms returned nothing usable).
- git is invoked with a fixed environment (`LC_ALL=C`, `GIT_CONFIG_NOSYSTEM=1`, no pager,
  no external diff, no color) and NUL-separated output (`-z`) so paths with spaces, quotes or
  non-ASCII bytes parse exactly. Rename detection uses `-M`; renamed files keep both paths.
- The local walker uses `--no-merges`: merge commits are not task candidates. Merged pull
  requests are covered separately by the GitHub walker (squash and rebase merges).
- Scoring is a transparent linear model: `score = sum(weight_i * feature_i)`. Every exported
  candidate carries each feature's raw value, weight and contribution, so a reviewer can see
  exactly why it ranked where it did. Weights have defaults in code and can be overridden in
  `commitminer.toml`. No LLM is in the loop; everything is deterministic and offline by
  default. The only network access is the optional GitHub walker (`GITHUB_TOKEN` from the
  environment, never committed; `.env.example` documents it).
- Demo repository: `hukkin/tomli` (MIT, a small TOML parser with `src/` and `tests/`,
  about 312 non-merge commits). Its history is recorded once into `examples/` and replayed,
  so `make demo`, the Docker demo and CI are offline and reproducible. A copy of tomli's
  LICENSE and the source URL and head commit sit next to the recording, because the recorded
  patches contain tomli code. Measured with `git log --no-merges -M -p | wc -c` on a clone:
  about 4.2 MB of patch text, mostly TOML test data, so the recording keeps full numstat for
  every file but patch text only where later slices need it, and must stay under the 512 KB
  pre-commit large-file limit (gzip if needed).
- The dedupe ledger is SQLite through the standard library (`sqlite3`), one file per team,
  so it needs no server and can be shared on a network drive or in CI artifacts.
- Coverage gate: 90% line+branch (`fail_under` in pyproject.toml).
- Confidentiality: all demo data comes from the public MIT repository above or from
  synthetic git repositories built by the tests. The name guard is a local pre-push hook only.

### Decisions made while building the core

- "Source and tests together" is a hard filter, not a score feature: every candidate has
  it, so as a feature it would add the same constant to every score. The score instead has
  five features (`small_diff` 3, `test_lines_added` 3, `linked_reference` 2, `fix_keyword`
  1, `focused_source` 1; the weights sum to 10).
- A new reason code `source-unchanged` rejects commits whose source files were only
  renamed (or changed mode, or are binary): the demo's "move to src layout" commit ranked
  6th before this.
- `--max-lines` counts source and test lines only; docs, changelog and CI churn does not
  make a task harder.
- The tomli recording is 746733 bytes as JSONL (5292 file entries, mostly TOML test data),
  over the 512 KB pre-commit limit, so recordings ending in `.gz` are gzipped with
  `mtime=0` (63697 bytes). No patch text is recorded yet.
- Recordings drop the author and committer fields; messages stay verbatim so the file can
  be re-verified against upstream (`make verify-recording`, also run in CI; it matched on
  macOS git 2.50 and on the Ubuntu runner).
- git 2.50 prints UTC author dates as `Z`; the parser normalises them to `+00:00` so
  recordings do not depend on the git version.
- The walker ignores global git config (`GIT_CONFIG_GLOBAL=/dev/null`) for determinism,
  so `safe.directory` cannot be set there; in Docker on Linux, mine a bind-mounted clone
  with `-u "$(id -u):$(id -g)"` (CI does this on the repository itself).

### Decisions made while building slice 1

- Categories: `other` stays as the fallback and also holds tooling (benchmarks including
  Rust `benches/`, scripts, examples, fuzzers): none of them are the tests a task runs, so
  counting `benches/` as test would let benchmark edits pass the test filter.
- Lockfiles moved from config to generated (tomli's `poetry.lock` is the only change in the
  demo; its ranking is unchanged).
- Rule order: vendored, test directories, generated, test file names, Java main source set,
  config, docs, tooling, source, prose names. Test directories come before generated so
  golden files under `testdata/` stay test data. The table test found two ordering bugs
  that shaped this: `src/main/java/com/example/` matched the `examples`-style tooling rule
  (fixed with a `java-main-dir` rule), and prose names such as `history*` and `license*`
  ahead of source made `history.py` a docs file (fixed by splitting `docs-file`, by
  extension, from `docs-name`, checked after source).
- Globs are compiled by a small translator (`**`, `*`, `?`, `[...]`, `{a,b}`), because
  `fnmatch` has neither `**` nor braces. `dir` rules match any path component.
- Java test class names are matched case-sensitively (`*Test.java` must not match
  `Latest.java`); every other rule is case-insensitive.
- Content signals depend only on a file's bytes and language, so the walker computes them
  once (one `git cat-file --batch` process, first 1 MiB of each changed code file) and
  recordings store them per file, only when present: the bundled tomli recording stayed
  byte-identical (checked with `cmp` against a fresh recording, and by CI).
- Rust inline tests: a file with a `#[cfg(test)]` module gets `rust-tests-added` when its
  `#[test]` attribute count grew against the parent version; that satisfies the filter's
  test requirement. Its lines stay source lines until slice 2 splits hunks. Separate Rust
  test-module files (`tests.rs`, `*_tests.rs`) are a test rule.
- `commitminer.toml` holds only `[classify]` for now (`rules` checked first, `disable` by
  id), with a strict schema: unknown tables, keys, categories, languages or signals are
  errors. Slice 2 adds weights to the same file.
- Real-history check (live clones, not bundled): dtolnay/semver (Rust) went from 0 to 50
  candidates (18 with `--no-content`), spf13/pflag (Go) from 0 to 97.
- Review fixes after slice 2: `java-main-dir` came before the tooling rule but after the
  vendored, test-directory and test-name rules, so junit5's `src/main/java/.../Test.java`
  was a test and grpc-java's `examples/src/main/java/` was source. Replaced by two general
  mechanisms: `dir` rules skip Java package directories below `src/<set>/java/`, and a rule
  may list `unless` path globs (`java-test-file` does not apply under `src/main/`). Also
  new: `rust-build-script` (Cargo `build.rs` outside `src/` is config; it was the only
  "source" of two semver candidates) and `js-spec-dir` (Jasmine's default `spec/`, JS/TS
  only). Globs are now checked when a rule is built and matched without regex
  backtracking across components.

### Decisions made while building slice 2

- One `git log` call carries numstat and the patch (`-p --unified=0`). The patch of a
  commit is one token after the numstat terminator (see the slice 4 notes for NUL bytes
  inside patches); its file blocks come in
  numstat order and are matched by position, with every block's +/- counts checked against
  numstat (no mismatch on tomli, semver or pflag). The walker now streams the output
  (Popen, 64 KiB reads, stderr to a temporary file, watchdog timer) instead of buffering it,
  because patch text is about 20 times larger than numstat.
- Patch shape is pinned on the command line (`--diff-algorithm=myers`,
  `--inter-hunk-context=0`, `--indent-heuristic`): repository config such as
  `diff.algorithm` or `diff.interHunkContext` would otherwise change hunk counts.
- Recordings store measurements, not patch text (tomli's patch text is about 4.2 MB).
  Fields equal to their defaults are left out; the tomli recording grew from 63697 to
  67715 bytes. The recording format version stays 1: `patch` is optional and old
  recordings load (their commits simply have no patch data, which features report as
  "unknown: no patch data" instead of guessing).
- "Cosmetic" is judged per hunk on the sequence of normalised code lines (trailing comments
  and blank lines dropped; outside Python also leading and trailing whitespace). Inner
  whitespace is kept: semver `5e87530d55` changes a format string's spaces, a real
  behaviour change. On the three real histories every `source-cosmetic` subject was listed
  and the doubtful ones read with `git show --unified=0` (3 each on tomli, semver and
  pflag): all touched only comments, doc comments, lint pragmas or formatting.
- Rust test modules are found with a small lexer (block comments nest, strings, raw
  strings, char literals versus lifetimes) that matches the braces of each
  `#[cfg(test)]` item. A regex-per-token lexer replaced a char-by-char one after profiling
  (semver walk 1.07 s to 0.54 s at the time; same results).
- A Rust source file counts as changing tests when its `#[test]` count grew (slice 1) or
  lines inside its test modules were added (new). Edits only inside test modules reject the
  commit as `source-unchanged`.
- Hard filters: `too-large` became `oversize` and also caps source files (10 by default);
  `docs-only` and `generated-only` split out of `no-source`; `source-cosmetic` is new.
  Filters live in `filters.py` with a description per code.
- Score and difficulty are separate. The score ranks (6 features; `test_lines_added` went
  from 3 to 2 to make room for `added_assertions` at 1, keeping the sum at 10). Difficulty
  (files 1, hunks 3, lines 3, cross_file 2, public_api 1) measures the fix on source code
  only and gives a band; it never changes the ranking. Band thresholds 2 and 4.5 were
  anchored on two hand-computed examples (a one-file, 3-hunk, 20-line fix is 1.7, easy; a
  public-API change over three source files, 5 hunks and 40 lines is 5.1, hard) and checked
  against the spread on the three histories.
- `commitminer.toml` gained `[filter]`, `[score]`, `[score.weights]`, `[difficulty]` and
  `[difficulty.weights]`; command-line limits override the file. The export schema went to
  version 2 (difficulty, public_api, per-file patch).
- `explain` works on a clone (any revision, merges refused because `git log --no-merges -1`
  would silently return an ancestor) or on a recording (sha prefix of at least 4 hex
  digits). Golden files pin text and JSON output for fixed synthetic commits built with
  fixed dates, so shas are reproducible; `UPDATE_GOLDEN=1` refreshes them.

### Decisions made while building slice 3

- Five review findings were fixed first, each with a regression test: the Java main source
  set (see slice 1 notes above), Cargo `build.rs`, Jasmine's `spec/`, a generated-header
  signal that fired on comments merely mentioning generated code (tket2's and shimmy's
  crate roots), and unchecked `commitminer.toml` globs (crash on `[z-a]`, silent no-op on a
  `/` in `dirs`, exponential backtracking on repeated `**/`). The glob matcher is now a
  module of its own: `fnmatch.translate` per component (atomic groups) and a first-fit
  block matcher across `**` segments, plus a plain-component precheck that brought tomli
  replay back from 0.21 s to 0.17 s.
- Hunk normalisation collapses whitespace runs instead of deleting all whitespace. The first
  version deleted it, and semver's top candidate (`"{} {}"` to `"{}{}"`) and pflag's gofmt
  commit had no hash left; with collapsing every one of the 202 candidates of tomli, semver
  and pflag has a fingerprint and re-indented copies still match.
- The fingerprint covers source and test hunks only; docs, config and CI hunks differ
  between forks and backports. Hunk hashes are 64 bits (16 hex digits of SHA-256): with a
  million hunks in a ledger the chance of any collision is about 3e-8.
- Recordings store `hunk_hashes` per file whenever computed, even when empty, so an old
  recording (no key) reads as "unknown" rather than "no hunks". The recording format stays
  version 1; the tomli recording grew from 67715 to 125311 bytes and was re-recorded (CI's
  byte-for-byte check passed on Ubuntu).
- Overlap is measured against the smaller fix (`shared / min(|A|, |B|)`), so a squash that
  contains the whole fix overlaps, and the default threshold is 0.5. Measured on the three
  real histories with an in-run check: 2 pairs reach it (semver's re-landed change, 45 of
  46 hunks; pflag's same one-line fix in two files, 1 of 2), 11 pairs share one hunk at
  25% or less.
- Ledger: one SQLite file, `PRAGMA user_version` for the schema version with a migration
  table (tested with a monkeypatched version 2), `PRAGMA application_id` to refuse other
  SQLite files, the fingerprint version in a `meta` table. Rollback journal, not WAL, so it
  works where WAL does not (network drives). Statuses `proposed` and `claimed` only.
- Claims: `BEGIN IMMEDIATE`, check, insert; `UNIQUE (fingerprint)` and `UNIQUE (repo, sha)`
  as the backstop. A test starts 8 threads with their own connections behind a barrier:
  exactly one claim wins.
- Checks never write: `mine --ledger` and `ledger check` copy the ledger into an in-memory
  database (backup API) and add each checked candidate there, so in-run duplicates use the
  same query. `mine --ledger` creates an empty ledger if the file is missing, like every
  ledger command.
- `ledger add` reads the `mine --out` file (schema version 3, which adds `fingerprint` and
  `ledger`) instead of re-walking, so what is claimed is exactly what was reviewed; it
  checks that the patch hash matches the listed hunks. Exit code 1 when a candidate is
  refused (and for `ledger check` when one is not new) lets scripts detect a lost race.
- The demo repositories are built by a standard-library script with fixed dates
  (`examples/ledger/build_repos.py`), shared by `make demo-ledger`, the CLI tests, CI and
  the Docker job; the shas were identical on macOS and in the Linux image.

### Decisions made while building slice 4

- Review fixes first, each with a regression test. (1) A type change (a file that became
  a symlink or a submodule, or back) is one numstat entry but two patch blocks under one
  `diff --git` line; `parse_patch` merges a deletion block followed by a creation block
  with the same header. serde (3542 commits) aborted on its "Update license symlinks"
  commit and now mines. (2) Patch text can contain NUL bytes (git only checks the first
  8000 bytes, a `diff` attribute forces text, hunk headers copy a file line as function
  context). Patch text ends with a newline and a NUL in it never follows one (every patch
  line starts with a marker), so the parser rejoins tokens until one ends with a newline.
  (3) `--submodule=short`, `--ignore-submodules=none` and `-O/dev/null` pin the submodule
  format, visibility and file order; `GIT_DIFF_OPTS`, `GIT_EXTERNAL_DIFF`, `GIT_CONFIG_*`
  and the repository-selecting variables (`GIT_DIR`, `GIT_WORK_TREE`, ...) are removed
  from git's environment. tomli, semver and pflag mine to byte-identical JSONL after
  these three fixes.
- (4) A changed line starting with `*` was always a comment, so operator-first
  continuations (`* height`, the default of rustfmt and google-java-format) made real
  fixes `source-cosmetic`; `strip_comment` also cut JavaScript lines at the `//` of a regex
  literal. The Rust test-module lexer became a C-like lexer for Rust, Go, Java and
  JavaScript/TypeScript (text blocks, Go raw strings, template literals, rune and char
  literals, regex literals), and `comment_regions` lists the lines that start inside a
  block comment. A `*` line is a comment only when the file version it belongs to puts it
  inside one. The contents are lexed only for files whose hunks have `*` lines on that
  side, and the parent version is read only then (or for Rust test modules). Without
  contents the rule errs towards code: only a comment opened in the same hunk, a bare `*`
  or a leading `*/` counts. Checked on six real histories (tomli, semver, pflag, serde,
  gson, ky; 7363 commits): no verdict changed, and two gson files gained one code line
  each (`*/package ...` after a license header, which was dropped before). gson mines in
  3.8 s either way.
- A merged pull request becomes a `Commit` with a `PullRequest` attached (merge commit sha,
  GitHub's `base.sha` as the only parent, merge date, title and description as the
  message), so filters, score, difficulty, fingerprints, ledger and export are shared
  instead of duplicated. `base.sha` follows the usual convention for tasks built from pull
  requests; its caveat (it can be ahead of the branch point) is a known issue.
- Endpoints: `pulls?state=closed&sort=updated&direction=desc&per_page=100` (merged ones
  kept, `--limit` of them), then per pull request `files` and `commits`. Files come first,
  so one with more than `--max-files` (300) changed files costs at most 3 requests and is
  skipped (tomli's #278 has 1397). The list endpoint has no `changed_files`, so it cannot
  be skipped earlier.
- GitHub's per-file patches have three context lines. `hunks_from_unified` splits them at
  context lines and numbers an empty side by the line before it, as git does; a test
  compares it with `git diff --unified=0` on a real repository. All six tomli candidates
  have the same fingerprint (and score) as the squashed commits they became, so the
  ledger flags them as duplicates of the mined history.
- Linked issues: GitHub's nine closing keywords with `#N`, `owner/repo#N` or an issue URL,
  in the description or commit messages (not the title, and not pull-request URLs). The
  scorer reads them (`linked_reference` 1.0), counts the pull request itself as a bare
  reference (0.5, like a squashed `(#123)` subject, which kept tomli's scores equal
  across both sources), and lets a bug/fix/regression/crash label set `fix_keyword`.
- The client is httpx (new dependency) with an injectable transport and clock. Record and
  replay are transports, so the cache, rate-limit and retry code runs unchanged offline.
  Fixtures are one JSON file per request key (method, path, sorted query) with a list of
  answers served in order, so sequences such as 403-then-200 can be scripted. Bodies are
  trimmed to the fields CommitMiner reads (a raw page of 100 pull requests is 1.48 MB;
  the tomli fixtures are 396 KB in 52 files), headers to validators, links and rate-limit
  counters; request headers are never stored. The recorder hands the client the trimmed
  answer, so live and replayed runs see the same data (checked: identical JSONL).
- Rate limits: the budget from `X-RateLimit-*` is honoured before each request;
  `Retry-After` on 403/429 first, then the reset when the budget is zero, then a
  secondary-limit backoff from 60 s (GitHub's guidance), then 1, 2, 4 s for 5xx and
  network errors; `--max-wait` (300 s) turns a long wait into an error with a
  `GITHUB_TOKEN` hint. No jitter, so the waits are testable with a fake clock.
- The ETag cache defaults to `~/.cache/commitminer/github` (XDG), is off with `--replay`
  unless `--cache-dir` is given (the demo uses one to show 52 of 52 answers as 304), and
  is refused with `--record`, which must capture full answers. Live, a second cached run
  left the rate limit where the first left it (4700 of 5000).
- `prs` imports httpx and the client lazily, so the other commands keep their start-up
  time (replay of the tomli recording 0.18 to 0.20 s, 0.21 to 0.22 s with eager imports).
- Export schema 4 adds `pull_request`; the ledger reads schema 3 and 4. Tables name pull
  requests `#123` in a `pull` column.

### Decisions made while building slice 5

- Three review findings were fixed first, each with a regression test. (1) Opening a
  ledger decided whether to create or migrate the schema from reads made before
  `BEGIN IMMEDIATE`: processes opening one new file at once could fail with "not a
  commitminer ledger", and every opener of an old-schema file re-applied the migration.
  `_setup` re-reads `application_id` and `user_version` under the lock and runs the whole
  migration chain in one transaction; a hook test runs "the other process" between the
  first look and the lock, and five spawned processes behind a barrier create (or
  migrate) one ledger exactly once. (2) A fix that only changed whitespace inside a line
  (`f"{a}  {b}"` to `f"{a} {b}"`) collapsed to identical lines and had no hunk hash, so
  the commit had no fingerprint, `--new-only` dropped it and `ledger add` refused it.
  Such a hunk is hashed a second way with inner whitespace kept (`FINGERPRINT_VERSION`
  2; the tomli recording was re-recorded, 3 of 5292 file entries gained a hash). (3)
  SQLite errors after opening (read-only file, a lock held longer than the timeout)
  escaped as tracebacks, and every failure exited 1, the code that means "refused" or
  "not new". They are `LedgerError` now, every error exits 2, and only `ledger add`
  creates a missing ledger (`ledger check` on a mistyped path used to check against an
  empty one and report everything new; an empty file still counts as an empty ledger).
- Likely fail-to-pass test ids need the test functions' line ranges, which the walker
  can compute because it already reads every changed code file for signals:
  `testids.py` finds them (Python by indentation with decorators and classes, the other
  languages by brace matching with the existing lexer) and `analyze` names the functions
  whose lines a hunk's new side overlaps; a deletion-only hunk is placed by the line git
  numbers it with. Without contents (pull requests, `--no-content`) only test
  definitions among the added lines are named, so Rust ids lose their module path there.
  Names are stored per file in recordings (`tests`, only when non-empty; format version
  stays 1) and joined with the path at export time. The tomli recording grew from 125342
  to 126060 bytes; 30 of its 44 candidates have ids, the rest change only test data.
- Export schema 5: the first line is a run record (source, URL, funnel, every rejection,
  ledger summary, settings) so the report has the funnel and the rejected commits without
  a sidecar file; candidates carry `repo_url` (`--url`, the recording's URL, the clone's
  `remote.origin.url`, or the GitHub web URL) and `fail_to_pass`. The JSON Schema is
  committed in the package (`schemas/export-v5.schema.json`, printed by
  `commitminer schema`) with `additionalProperties: false` everywhere; `jsonschema` is a
  dev dependency only, and the tests validate the tomli, pull-request and ledger exports.
  The ledger reader accepts schema 3 to 5 and skips the run record.
- The report is one document (headings, paragraphs, tables, collapsible sections) built
  from the export and rendered as Markdown or HTML, so both say the same thing. Links
  only for http(s) URLs (a `user@host:path` remote is converted; `javascript:` is text),
  entities in HTML, backslashes before Markdown punctuation, inline CSS and no scripts.
  Golden files pin both formats on the ledger demo's fork mined against a ledger under
  a fixed clock. `--top` limits candidates, not rejections.

## Core (deliverable)

- [x] Core: done on 2026-09-30. 127 tests, 100% line and branch coverage, CI green
  (checks, recording re-verified from GitHub, docker build + demo). Deviations from the
  outline below are listed under "Decisions made while building the core".

The smallest end-to-end path, from a git history to a ranked JSONL file:

- `commitminer mine <clone>`: a history walker over a local clone
  (`git log --no-merges -M -z --numstat` with a fixed format) that yields typed commit
  records: sha, parents, author date, subject, body, and per-file added/deleted line counts
  with old and new paths for renames. Binary files are marked, not counted.
- A first path-convention classifier for Python (source, test, docs, config, other), as a
  small rule list in code (`tests/`, `test_*.py`, `*_test.py`, `conftest.py`, `docs/`,
  `*.md`, `pyproject.toml`, and so on).
- A candidate filter (at least one source file and at least one test file changed, and no
  more than a configurable number of changed lines) and a first transparent scorer with four
  features: source+tests together, diff size (smaller is better), test lines added, linked
  issue or pull-request reference in the message (`#123`, `fixes #123`). Each candidate
  records every feature's value, weight and contribution.
- `commitminer record <clone> --out <file>` writes the walked history to a recorded file, and
  `commitminer mine --history <file>` replays it, with the same code path after parsing.
- JSONL export sorted by score (ties broken by date, then sha), plus a one-line-per-candidate
  table on stdout.
- `examples/tomli/`: the recorded tomli history, its LICENSE copy and provenance.
- `make demo` mines the recorded tomli history and prints the top candidates; the Docker
  image runs the same demo; CI runs both.
- Tests: synthetic git repositories built in `tmp_path` (renames, spaces in paths, binary
  files, empty commits), parser edge cases, filter and scorer golden values, CLI tests.
- README with real demo output, the commands that produced every number, and Known issues.

## Slices

- [x] 1. Multi-language classifier with a tested rule table. Done on 2026-09-30: 34 rules
  in `docs/rules.md`, `classify` and `rules` commands, `commitminer.toml` overrides, content
  signals read while mining; 440 tests, 100% coverage. See "Decisions made while building
  slice 1".
  Extend classification to Python, Rust, JavaScript/TypeScript, Go and Java with categories
  source, test, docs, config, generated and vendored. Rules live in one ordered table (rule
  id, languages, path glob or content signal, category, rationale). Path conventions:
  `__tests__/`, `*.test.ts`, `*.spec.js`, `*_test.go`, `src/test/java/`, Rust `tests/` and
  `benches/`, `vendor/`, `third_party/`, `node_modules/`, lockfiles. Content signals:
  generated-code headers (`Code generated ... DO NOT EDIT`, `@generated`), minified
  JavaScript, and Rust in-file `#[cfg(test)]` modules (a source file that also changes tests).
  `commitminer classify <path>...` prints the category and the rule that matched. Per-repo
  overrides in `commitminer.toml`. Table-driven tests: every rule has a positive and a
  negative example, and a test fails if a rule has no example.
- [x] 2. Patch-level difficulty features with per-feature contributions. Done on
  2026-09-30: patch measurements while walking, hard filters with 8 reason codes, a score
  with 6 features and a difficulty estimate with 5 and a band, weights from
  `commitminer.toml`, `commitminer explain`; 596 tests, 100% coverage. See "Decisions made
  while building slice 2".
  Walk patches (`-p --unified=0`) to count hunks; add difficulty features: files, hunks and
  lines changed, cross-file edits (distinct source files touched), public API touched
  (per-language signals such as top-level `def`/`class` without a leading underscore,
  `pub fn`, exported Go identifiers, `export`, `public`), and added assertions in tests
  (`assert`, `assert_eq!`, `expect(`, `t.Errorf`, `assertEquals`). Hard filters (docs-only,
  generated-only, oversize) are reported separately from soft scoring, with a reason code.
  Weights load from `commitminer.toml`. `commitminer explain <sha>` prints the contribution
  table for one commit and a difficulty band (easy, medium, hard). Golden tests pin the
  explanations for fixed synthetic commits.
- [x] 3. Patch fingerprints and a SQLite dedupe ledger. Done on 2026-09-30: hunk hashes
  measured while walking, candidate fingerprints, a versioned SQLite ledger with atomic
  claims, `ledger add|check|list`, `mine --ledger` (with `--min-overlap`, `--new-only`),
  export schema 3, `make demo-ledger`; 752 tests, 100% coverage. See "Decisions made while
  building slice 3".
  A patch fingerprint that ignores whitespace, file paths and renames, and hunk line
  numbers: normalise each changed line, hash per hunk, and hash the sorted hunk hashes for
  the whole patch; keep the per-hunk set for partial-overlap detection (a cherry-pick that
  needed a small conflict fix). A SQLite ledger with a versioned schema records proposed
  candidates (repo, sha, fingerprint, status, first seen); `commitminer ledger add|check|list`
  and `mine --ledger` mark exact duplicates and partial overlaps across repositories, forks
  and cherry-picks, so the same fix is never proposed twice. Claiming a candidate is atomic
  (unique constraint in one transaction), so two authors cannot take the same fix. Tests
  build a repository with a cherry-pick, a re-indented copy and a rename-only variant.
- [x] 4. GitHub merged pull-request walker with cache, rate limits and recorded fixtures.
  Done on 2026-09-30: `commitminer prs`, httpx client with an ETag cache, rate limits and
  retries on an injectable clock, record/replay transports, 52 recorded tomli responses,
  `make demo-prs`, export schema 4; four review fixes first (type changes, NUL bytes in
  patches, pinned git settings, `*` lines); 892 tests, 100% coverage. See "Decisions made
  while building slice 4".
  `commitminer prs OWNER/REPO` lists merged pull requests through the REST API (httpx), with
  their commits, changed files and linked issues (closing keywords in the body). An on-disk
  cache keyed by URL stores ETags, so repeat runs use conditional requests. Rate-limit
  handling reads `X-RateLimit-Remaining` and `X-RateLimit-Reset`, honours `Retry-After` on
  403/429, and backs off with an injectable clock so tests do not sleep. `--record <dir>`
  saves responses as fixtures and `--replay <dir>` serves them offline; tests use recorded
  fixtures only. Pull-request metadata (number, linked issues, labels) feeds the scorer.
- [x] 5. Candidate export schema and Markdown/HTML report. Done on 2026-09-30: three
  review fixes first (ledger set-up under the write lock, whitespace-inside-a-line
  fingerprints, wrapped SQLite errors and exit code 2), then `testids.py`, export schema
  5 with a run record and `fail_to_pass`, a committed JSON Schema validated in tests and
  CI, `commitminer report` (Markdown and self-contained HTML, golden files),
  `commitminer schema`, `make demo-report`; 930 tests, 100% coverage. See "Decisions
  made while building slice 5".
  A versioned JSONL schema for downstream environment builders (repository URL, base commit,
  fix commit, source and test files, likely fail-to-pass test ids guessed from test
  functions touched in the diff, feature breakdown, fingerprint, ledger status), with a JSON
  Schema file committed and validated in tests. `commitminer report` renders a Markdown
  report and a self-contained HTML report (no external assets, all text escaped): a funnel
  (walked, classified, filtered, deduplicated, exported), the ranked table with per-feature
  contributions, and the rejected commits by reason. Golden-file tests.
- [ ] 6. Multi-repository batch mining with incremental resume.
  `commitminer batch commitminer.toml` mines several repositories (local clones, recorded
  histories or GitHub pull requests) into one ledger and one report. A per-repository
  watermark in the ledger means a re-run walks only new commits. Collisions across the
  batch (the same fix in two repositories) are reported. A second recorded demo history in
  another language (a small MIT or Apache-2.0 Rust or Go repository) exercises the
  multi-language classifier and cross-repository dedupe in `make demo`.
