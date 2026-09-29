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

- [ ] 1. Multi-language classifier with a tested rule table.
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
- [ ] 2. Patch-level difficulty features with per-feature contributions.
  Walk patches (`-p --unified=0`) to count hunks; add difficulty features: files, hunks and
  lines changed, cross-file edits (distinct source files touched), public API touched
  (per-language signals such as top-level `def`/`class` without a leading underscore,
  `pub fn`, exported Go identifiers, `export`, `public`), and added assertions in tests
  (`assert`, `assert_eq!`, `expect(`, `t.Errorf`, `assertEquals`). Hard filters (docs-only,
  generated-only, oversize) are reported separately from soft scoring, with a reason code.
  Weights load from `commitminer.toml`. `commitminer explain <sha>` prints the contribution
  table for one commit and a difficulty band (easy, medium, hard). Golden tests pin the
  explanations for fixed synthetic commits.
- [ ] 3. Patch fingerprints and a SQLite dedupe ledger.
  A patch fingerprint that ignores whitespace, file paths and renames, and hunk line
  numbers: normalise each changed line, hash per hunk, and hash the sorted hunk hashes for
  the whole patch; keep the per-hunk set for partial-overlap detection (a cherry-pick that
  needed a small conflict fix). A SQLite ledger with a versioned schema records proposed
  candidates (repo, sha, fingerprint, status, first seen); `commitminer ledger add|check|list`
  and `mine --ledger` mark exact duplicates and partial overlaps across repositories, forks
  and cherry-picks, so the same fix is never proposed twice. Claiming a candidate is atomic
  (unique constraint in one transaction), so two authors cannot take the same fix. Tests
  build a repository with a cherry-pick, a re-indented copy and a rename-only variant.
- [ ] 4. GitHub merged pull-request walker with cache, rate limits and recorded fixtures.
  `commitminer prs OWNER/REPO` lists merged pull requests through the REST API (httpx), with
  their commits, changed files and linked issues (closing keywords in the body). An on-disk
  cache keyed by URL stores ETags, so repeat runs use conditional requests. Rate-limit
  handling reads `X-RateLimit-Remaining` and `X-RateLimit-Reset`, honours `Retry-After` on
  403/429, and backs off with an injectable clock so tests do not sleep. `--record <dir>`
  saves responses as fixtures and `--replay <dir>` serves them offline; tests use recorded
  fixtures only. Pull-request metadata (number, linked issues, labels) feeds the scorer.
- [ ] 5. Candidate export schema and Markdown/HTML report.
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
