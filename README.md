# CommitMiner

[![CI](https://github.com/vipul21435/commitminer/actions/workflows/ci.yml/badge.svg)](https://github.com/vipul21435/commitminer/actions/workflows/ci.yml)

Mine and rank candidate fail-to-pass tasks from real repository history, so task authors
spend time only on commits that can become good tasks.

A fail-to-pass task is built from a real fix: the tests added or changed by a commit fail on
the parent commit and pass on the fix. Most commits in a history cannot become such a task
(docs, CI, release bumps, refactors, comment edits, test-only changes, huge features).
CommitMiner walks the history, classifies every changed file, measures every file's patch,
drops the commits that cannot work (with a reason code), and ranks the rest with a
transparent score. A separate difficulty estimate puts each candidate in an easy, medium or
hard band. Each feature's value, weight and contribution are printed and exported, so a
reviewer can see why a commit ranked where it did. Every candidate gets a patch
fingerprint, and a shared SQLite ledger marks fixes that were already proposed: the same fix
in a fork, a cherry-pick, a re-indented or moved copy. Merged pull requests can be mined
from the GitHub REST API the same way (ETag cache, rate-limit handling, recorded fixtures),
and their fixes collide in the ledger with the commits they became. The output is JSON
Lines for downstream environment builders, with a committed JSON Schema, the likely
fail-to-pass test ids of each candidate, and a Markdown or self-contained HTML report.

CommitMiner proposes and ranks. It does not build environments or run the tests; verifying
the flip is the downstream builder's job.

## What works today

- **History walker** over a local clone: one streamed
  `git log --no-merges -M -z --numstat -p --unified=0` call with a fixed environment and
  command-line config (diff algorithm, inter-hunk context, indent heuristic, submodule
  format and file order pinned; `GIT_DIFF_OPTS`, `GIT_CONFIG_*` and `GIT_DIR` from the
  caller dropped), parsed one commit at a time from NUL-separated output. Renames keep both
  paths, binary files have no line counts, and paths with spaces or non-UTF-8 bytes parse
  exactly. Each file's patch block is matched to its numstat entry and its line counts are
  checked; type changes (a file that became a symlink or a submodule) and NUL bytes inside
  text patches are handled.
- **GitHub merged pull-request walker** (`commitminer prs OWNER/REPO`): the `--limit` most
  recently updated merged pull requests, each with its commits and changed files, through
  the REST API (httpx). An on-disk ETag cache turns repeat runs into conditional requests
  (`304 Not Modified` is served from the cache; with a token GitHub does not count a 304
  against the rate limit, without one it does). `X-RateLimit-Remaining`/`Reset` are read
  from every answer, `Retry-After` is honoured on 403 and 429, secondary limits back off
  60, 120, 240 s, server and network errors 1, 2, 4, 8 s, all through an injectable clock;
  a wait over `--max-wait` (300 s) fails instead of hanging. `--record DIR` saves every
  response as a fixture, `--replay DIR` answers from fixtures without network. Linked issues come from closing keywords in the
  description and commit messages; labels and linked issues feed the score. GitHub's
  per-file patches (three context lines) are split into `--unified=0` hunks, so pull
  requests get the same measurements and fingerprints as commits. `GITHUB_TOKEN` is
  optional.
- **Patch measurements per file** (Python, Rust, JavaScript/TypeScript, Go, Java): hunks;
  code hunks and code lines, leaving out blank lines, comment-only lines and hunks that
  only change comments or (outside Python) indentation; added assertion lines (`assert`,
  `assert_eq!`, `expect(`, `t.Errorf`, `assertEquals`, ...) net of identical deleted ones;
  public declarations touched (top-level `def`/`class` without a leading underscore,
  `pub` items, exported Go identifiers, `export`, `public`); and for Rust, the lines inside
  `#[cfg(test)]` modules, found with a small lexer that skips braces in comments, strings,
  raw strings and char literals. Those lines count as test lines, not source lines. A
  changed line that starts with `*` is a comment only when the same lexer puts it inside
  a `/* ... */` comment of that file version (Rust, Go, Java, JavaScript/TypeScript); an
  operator-first continuation such as `* height` (rustfmt's and google-java-format's
  style) is code. JavaScript regex literals such as `/^https?:\/\//` are not comments.
- **Record and replay**: `commitminer record` saves a walked history, measurements
  included, as deterministic JSON Lines (gzipped when the name ends in `.gz`);
  `commitminer mine --history` replays it through the same code path. Mining the live tomli
  clone and replaying its recording produce byte-identical JSONL (checked with `cmp`).
- **Multi-language file classifier**: one ordered table of 35 rules
  ([docs/rules.md](docs/rules.md), generated from the code) for Python, Rust,
  JavaScript/TypeScript, Go and Java. Each rule has an id, languages, a matcher (directory,
  file-name or path glob, or a content signal), a category (source, test, docs, config,
  generated, vendored, other) and a rationale; the first match wins and its id is reported.
  Every rule has positive and negative examples in a table-driven test, which fails when a
  rule has none. Java package directories below `src/<set>/java/` are not layout, so
  `com/shop/vendor/` is not vendored and junit5's `src/main/java/.../Test.java` is source.
  Globs are checked when a rule is built (a `/` in a directory or file-name pattern, an
  empty range such as `[z-a]`, an empty path segment are errors) and matched in linear time.
- **Content signals**: generated-code headers (`Code generated ... DO NOT EDIT`,
  `@generated`, or a comment that opens with "Auto-generated" or "This file was
  generated"; a comment that only mentions generated bindings does not count), minified
  JavaScript and Rust `#[cfg(test)]` modules, read through one
  `git cat-file --batch` process while walking. A Rust file whose `#[test]` count grew, or
  that gained lines inside its test modules, counts as a test change, so Rust fixes with
  inline tests become candidates.
- **Hard filters with reason codes**, checked in this order before any scoring: `empty`,
  `docs-only`, `generated-only` (generated or vendored files only), `no-source`,
  `source-unchanged` (renames, mode changes, binary files, or edits only inside Rust test
  modules), `source-cosmetic` (source changes touch only comments, blank lines or
  indentation), `no-test`, and `oversize` (more source+test lines than `--max-lines`, or
  more source files than `--max-source-files`).
- **Transparent score** that ranks candidates: six features in `[0, 1]` times weights that
  add up to 10: `small_diff` (3), `test_lines_added` (2), `added_assertions` (1),
  `linked_reference` (2: closing keyword 1.0, bare `#123` 0.5), `fix_keyword` (1),
  `focused_source` (1). Ranked by score, then newest, then sha.
- **Difficulty estimate** with an easy/medium/hard band, reported next to the score: five
  features whose weights add up to 10: `files` (1: source+test files), `hunks` (3: source
  code hunks), `lines` (3: source code lines), `cross_file` (2: source files with code
  changes beyond the first), `public_api` (1). Default bands: easy below 2, hard from 4.5.
- **`commitminer explain SHA`** for one commit, from a clone (`--repo`, any revision) or a
  recording (`--history`, sha prefix): the per-file table, the verdict with its reason code
  and meaning (and the broken limit for `oversize`), and for candidates both contribution
  tables with totals and the band thresholds. `--json` prints one object. Golden tests pin
  the explanations of fixed synthetic commits in all five languages.
- **`commitminer.toml`** at the repository root (or `--config`): classifier rules and
  disabled built-in rules, filter limits, feature caps, both weight sets and the band
  thresholds, with a strict schema (unknown keys, wrong types, negative weights and
  inverted bands are errors). Command-line limits win over the file. See
  [Configuration](#configuration).
- **`commitminer classify PATH...`** and **`commitminer rules`** show how paths are
  classified and the effective rule table.
- **Patch fingerprints** (version 2): every hunk of every walked patch gets a 64-bit hash
  of its changed lines with leading and trailing whitespace dropped, inner runs of
  whitespace collapsed and blank lines left out; the file path and the hunk's line numbers
  are not hashed. A hunk whose lines are equal after that (`"1  2"` to `"1 2"`, a change
  inside a string literal) is hashed with its inner whitespace kept, so it still has a
  fingerprint; only hunks that change nothing but indentation, trailing whitespace or blank
  lines have none. A candidate's fingerprint is its source and test hunk hashes, sorted,
  plus a patch hash over them, so a cherry-pick onto a shifted file, a re-indented copy and
  a renamed or moved file all get the same fingerprint, and a cherry-pick with a resolved
  conflict shares most of its hunks.
- **Dedupe ledger** (`commitminer ledger add|check|list`, `mine --ledger`): one SQLite file
  (standard library `sqlite3`, versioned schema) records proposed fixes by fingerprint with
  repository, sha, status, owner and first-seen time. A candidate is a `duplicate` (same
  patch hash), an `overlap` (shares at least `--min-overlap`, default 0.5, of the distinct
  hunks of the smaller fix), or `new`. Adding is one `BEGIN IMMEDIATE` transaction backed by
  a unique constraint on the fingerprint, so two authors cannot claim the same fix;
  checking never writes and also compares the candidates of one run with each other.
- **Likely fail-to-pass test ids**: the test functions a patch touches, found by their
  line ranges in the new file version (Python blocks by indentation with their decorators
  and classes; Rust, Go, Java and JavaScript/TypeScript by brace matching with the same
  lexer) and named the way each runner takes them: `tests/test_x.py::TestCase::test_y`,
  `src/lib.rs::tests::name`, `pkg/x_test.go::TestName`, `.../XTest.java::XTest#method`,
  `x.test.js::test name`. 30 of tomli's 44 candidates get ids (the other 14 change only
  `.toml`/`.json` test data). Without file contents (pull requests, `--no-content`) only
  test definitions among the added lines are named.
- **Export** (schema version 5, [JSON Schema](src/commitminer/schemas/export-v5.schema.json)
  committed, printed by `commitminer schema`, every export validated against it in the
  tests and in CI): a run record first (source, URL, funnel counts, every rejected commit
  with its reason, ledger summary, settings), then one candidate per line: repository URL,
  base and fix commits, source, test and inline-test files, likely fail-to-pass test ids,
  per-file category, rule, signals and patch measurements, line counts, public API
  touched, score and difficulty breakdowns, fingerprint, the ledger verdict with its
  matches, and for pull requests their number, URL, labels, linked issues, base, head and
  merge shas and commits. Plus a terminal table and per-candidate contribution tables.
- **Reports** (`commitminer report CANDIDATES.jsonl --out report.md|report.html`): from an
  export, a Markdown report or one self-contained HTML page (inline CSS, no scripts, no
  external assets, every string escaped): the funnel with the remaining count after each
  reason and each ledger verdict, the ranked table with one column per score feature
  contribution, a section per candidate with its files, test ids, fingerprint, ledger
  verdict and feature table, the rejected commits grouped by reason, and the settings.
  Commits and pull requests are linked when the export's URL is http(s) or a git remote.
  Golden files pin both formats.
- **Offline demo** on the recorded history of [hukkin/tomli](https://github.com/hukkin/tomli)
  (MIT, 312 non-merge commits), bundled in [`examples/tomli/`](examples/tomli/) with its
  license and provenance. CI re-records it from GitHub on every push and checks it still
  matches byte for byte. Next to it, 52 recorded API responses for tomli's 25 most
  recently updated merged pull requests (`examples/tomli/prs/`) drive `make demo-prs`.
- **Docker image** on digest-pinned `python:3.12-slim` and `uv` bases, running as uid 10001,
  with `LABEL project=commitminer`.

## Quickstart

Needs git, [uv](https://docs.astral.sh/uv/) and make.

```sh
git clone https://github.com/vipul21435/commitminer && cd commitminer
make install        # uv sync --locked + pre-commit hook
make demo           # mine the bundled tomli history offline
make demo-explain   # explain one candidate and one rejected tomli commit
make demo-classify  # classify the multi-language sample tree in examples/classify
make demo-ledger    # claim upstream fixes, then find them again in a release branch and a fork
make demo-prs       # rank tomli's merged pull requests from recorded GitHub responses
make demo-report    # export the tomli history and render Markdown and HTML reports
uv run commitminer mine /path/to/a/clone --out out/candidates.jsonl --ledger team.sqlite3
uv run commitminer report out/candidates.jsonl --out out/report.html
GITHUB_TOKEN=... uv run commitminer prs OWNER/REPO --limit 30 --out out/prs.jsonl
uv run commitminer ledger add team.sqlite3 out/candidates.jsonl --sha <sha> --owner <name>
uv run commitminer explain <sha> --repo /path/to/a/clone
```

Verified in a fresh clone of `cd8b012`: `make install` took 2.08 s, the first `make demo`
0.67 s and `make demo-ledger` 1.11 s (`/usr/bin/time -p`, warm uv cache, 8 GB Apple
Silicon Mac).

## Usage

```text
commitminer mine [REPO] [--history FILE] [--rev REV] [--max-count N] [--max-lines N]
                 [--max-source-files N] [--test-lines-cap N] [--top 10] [--explain 1]
                 [--out FILE] [--url URL] [--repo-name NAME] [--config FILE] [--no-content]
                 [--ledger FILE [--min-overlap 0.5] [--new-only]]
commitminer report CANDIDATES.jsonl [--out FILE] [--format markdown|html] [--top N]
commitminer schema
commitminer ledger add LEDGER CANDIDATES.jsonl [--sha SHA]... [--top N] [--owner NAME]
                       [--status claimed|proposed] [--min-overlap 0.5] [--allow-overlap]
commitminer ledger check LEDGER CANDIDATES.jsonl [--min-overlap 0.5] [--json]
commitminer ledger list LEDGER [--repo NAME] [--json]
commitminer explain SHA [--repo DIR | --history FILE] [--max-lines N] [--max-source-files N]
                    [--test-lines-cap N] [--config FILE] [--no-content] [--json]
commitminer record REPO --out FILE [--rev REV] [--max-count N] [--repo-name NAME] [--url URL]
commitminer prs OWNER/REPO [--limit 30] [--max-files 300] [--record DIR | --replay DIR]
                [--cache-dir DIR | --no-cache] [--api-url URL] [--max-wait 300] [--top 10]
                [--explain 1] [--out FILE] [--config FILE] [--max-lines N]
                [--max-source-files N] [--test-lines-cap N]
                [--ledger FILE [--min-overlap 0.5] [--new-only]]
commitminer classify PATH... [--root DIR] [--config FILE] [--no-content] [--json]
commitminer rules [--root DIR] [--config FILE] [--markdown]
commitminer version
```

`--no-content` skips reading file contents: classification uses path rules only and Rust
test modules are not found (their lines count as source). Patches are always measured.
`classify` paths are relative to `--root` (default: the current directory) and need not
exist; a path that is not a file there is classified by its path alone. `ledger add`
creates a missing ledger file (empty, with its schema); `ledger check`, `ledger list`,
`mine --ledger` and `prs --ledger` refuse a missing path, so a mistyped ledger cannot
report every candidate as new (an empty file counts as an empty ledger: `touch` one to
start).

Exit codes: 0 on success; 1 is an outcome, not an error (`ledger add` refused a candidate,
`ledger check` found one that is not new), so scripts can tell that a fix was already
taken; 2 for every error (usage, an unreadable file, git or GitHub failures, a ledger that
is read-only or locked longer than the 10 s timeout), printed as one `error:` line.

Output of `make demo` (the recorded tomli history, unedited). `diff` is the difficulty and
its band; the ranking uses only the score:

```text
hukkin/tomli: walked 312 commits, 44 candidates (easy 15, medium 17, hard 12), 268 rejected (docs-only 42, no-source 100, source-unchanged 1, source-cosmetic 6, no-test 115, oversize 4)

rank   score         diff  sha         date        lines  src  test  subject
   1    6.88    4.74 hard  5ab9ec926d  2021-05-28     62    1     1  NEW: Allow float parse func customisation...
   2    6.64  3.42 medium  948211d852  2021-06-05     75    1     8  FIX: Three odd cases
   3    6.57    0.74 easy  8b962e1349  2022-01-29     18    1     1  Raise a friendly `TypeError` for wrong fi...
   4    6.55  2.10 medium  2a2aa62f1b  2026-01-10     60    1     5  TOML 1.1: Allow newlines and trailing com...
   5    6.45    6.34 hard  149547d2ec  2024-11-27    113    2     2  Create binary wheels with mypyc (#242)
   6    6.29    7.20 hard  d1d6a8571b  2024-11-11    182    1     1  Add attributes to TOMLDecodeError. Deprec...
   7    6.13    0.71 easy  4e245a4bbb  2024-10-02     16    1     1  `tomli.loads`: Raise TypeError not Attrib...
   8    6.09    7.20 hard  67ec7d1052  2021-06-02    168    1     1  NEW: Print line and column in error messa...
   9    6.08  2.88 medium  b9cbbe27b5  2021-07-23     36    1     4  Add binary file object support to `load` ...
  10    6.06    0.53 easy  b7e1bcccb9  2026-04-10     25    1     1  Use Python 3.15 lazy import (#295)

#1 5ab9ec926d score 6.88, difficulty 4.74 (hard): NEW: Allow float parse func customisation (#2)
  score
    feature            value weight contrib  detail
    small_diff         0.845   3.00   2.535  62 of at most 400 source+test lines changed
    test_lines_added   0.875   2.00   1.750  35 test lines added (full value at 40)
    added_assertions   0.600   1.00   0.600  3 assertion lines added in tests (full value at 5)
    linked_reference   0.500   2.00   1.000  #2
    fix_keyword        0.000   1.00   0.000  no fix keyword in the subject
    focused_source     1.000   1.00   1.000  1 source file changed
    total                     10.00   6.885
  difficulty
    feature            value weight contrib  detail
    files              0.200   1.00   0.200  2 source+test files changed (full value at 10)
    hunks              1.000   3.00   3.000  10 source code hunks (full value at 10)
    lines              0.180   3.00   0.540  18 source code lines changed (full value at 100)
    cross_file         0.000   2.00   0.000  1 source file with code changes (full value at 5)
    public_api         1.000   1.00   1.000  def loads
    total                     10.00   4.740

wrote 44 candidates to out/tomli-candidates.jsonl
```

The first line of `out/tomli-candidates.jsonl`, with each feature and file on one line and
the file lists cut to the source and test file (the commit also changes `CHANGELOG.md` and
`README.md`). The fingerprint covers the 13 hunks of those two files; `ledger` is `null`
because the demo mines without `--ledger`, and `pull_request` because it is a commit:

```json
{
  "base": "37a543b74bb1633478aea9f3a6a450a550bdeb63",
  "date": "2021-05-28T23:10:06+02:00",
  "difficulty": {"band": "hard", "value": 4.74, "features": [
    {"contribution": 0.2, "detail": "2 source+test files changed (full value at 10)", "name": "files", "value": 0.2, "weight": 1.0},
    {"contribution": 3.0, "detail": "10 source code hunks (full value at 10)", "name": "hunks", "value": 1.0, "weight": 3.0},
    {"contribution": 0.54, "detail": "18 source code lines changed (full value at 100)", "name": "lines", "value": 0.18, "weight": 3.0},
    {"contribution": 0.0, "detail": "1 source file with code changes (full value at 5)", "name": "cross_file", "value": 0.0, "weight": 2.0},
    {"contribution": 1.0, "detail": "def loads", "name": "public_api", "value": 1.0, "weight": 1.0}
  ]},
  "features": [
    {"contribution": 2.535, "detail": "62 of at most 400 source+test lines changed", "name": "small_diff", "value": 0.845, "weight": 3.0},
    {"contribution": 1.75, "detail": "35 test lines added (full value at 40)", "name": "test_lines_added", "value": 0.875, "weight": 2.0},
    {"contribution": 0.6, "detail": "3 assertion lines added in tests (full value at 5)", "name": "added_assertions", "value": 0.6, "weight": 1.0},
    {"contribution": 1.0, "detail": "#2", "name": "linked_reference", "value": 0.5, "weight": 2.0},
    {"contribution": 0.0, "detail": "no fix keyword in the subject", "name": "fix_keyword", "value": 0.0, "weight": 1.0},
    {"contribution": 1.0, "detail": "1 source file changed", "name": "focused_source", "value": 1.0, "weight": 1.0}
  ],
  "files": [
    {"added": 35, "category": "test", "deleted": 6, "patch": {"api": ["def test_parse_float"], "asserts": 3, "code_added": 33, "code_deleted": 6, "code_hunks": 3, "hunks": 3, "test_added": 0, "test_deleted": 0}, "path": "tests/test_misc.py", "rule": "test-dir"},
    {"added": 13, "category": "source", "deleted": 8, "patch": {"api": ["def loads"], "asserts": 0, "code_added": 10, "code_deleted": 8, "code_hunks": 10, "hunks": 10, "test_added": 0, "test_deleted": 0}, "path": "tomli/_parser.py", "rule": "py-source"}
  ],
  "fingerprint": {"hunks": ["2238f1a042c06dea", "281157c378695ed6", "2d98fd8039a500da", "31087633dc36ca60", "42dc602a455f1507", "5a92b41459e49fb7", "66717315e93d09ff", "785747b132e589e7", "82c818dedf502363", "e1a9b9c9fabab1f0", "f1fa33414ce49364", "f2fac2deb39097db", "fa350b2a3bba7f1b"], "patch": "f10c534e4424fd9b", "version": 1},
  "inline_test_files": [],
  "ledger": null,
  "lines": {"changed": 62, "source_added": 13, "source_deleted": 8, "test_added": 35, "test_deleted": 6},
  "public_api": ["def loads"],
  "pull_request": null,
  "rank": 1,
  "repo": "hukkin/tomli",
  "schema_version": 4,
  "score": 6.885,
  "sha": "5ab9ec926d9dc1ef79e66215edd51285371fe8a0",
  "source_files": ["tomli/_parser.py"],
  "subject": "NEW: Allow float parse func customisation (#2)",
  "test_files": ["tests/test_misc.py"]
}
```

### Explaining one commit

`commitminer explain` shows how one commit was measured and judged. The file table lists,
per file, the numstat lines, hunks, code hunks (hunks that change code), lines inside Rust
test modules, and added assertion lines. From `make demo-explain` (unedited):

```text
$ commitminer explain 948211d852 --history examples/tomli/history.jsonl.gz
commit   948211d852a62ab78b20bae5d288f882822ca14f
base     bc1048993c13c8942e5cdf3faaf904453fa89416
date     2021-06-05T01:34:14+03:00
subject  FIX: Three odd cases
verdict  candidate: score 6.64 of 10, difficulty 3.42 of 10 (medium)
patch    fingerprint 3cbcfe3f86d6105c (16 source and test hunks)

  files
    category  rule               added   del hunks  code tests asserts  path
    config    build-config           9     9     2     2     0       0  .pre-commit-config.yaml
    docs      docs-file              7     0     1     1     0       0  CHANGELOG.md
    test      test-dir               4     0     1     1     0       0  tests/data/extras/invalid/array-of-tables/overwrite-array-in-parent.toml
    test      test-dir               3     0     1     1     0       0  tests/data/extras/invalid/table/redefine-1.toml
    test      test-dir               3     0     1     1     0       0  tests/data/extras/invalid/table/redefine-2.toml
    test      test-dir              11     0     1     1     0       0  tests/data/extras/valid/array/array-subtables.json
    test      test-dir               7     0     1     1     0       0  tests/data/extras/valid/array/array-subtables.toml
    test      test-dir               6     0     1     1     0       0  tests/data/extras/valid/array/open-parent-table.json
    test      test-dir               3     0     1     1     0       0  tests/data/extras/valid/array/open-parent-table.toml
    test      test-dir               8     0     2     2     0       1  tests/test_misc.py
    source    py-source             27     3     7     6     0       0  tomli/_parser.py

  score (ranks candidates)
    feature            value weight contrib  detail
    small_diff         0.812   3.00   2.438  75 of at most 400 source+test lines changed
    test_lines_added   1.000   2.00   2.000  45 test lines added (full value at 40)
    added_assertions   0.200   1.00   0.200  1 assertion line added in tests (full value at 5)
    linked_reference   0.000   2.00   0.000  no issue or pull-request reference
    fix_keyword        1.000   1.00   1.000  FIX
    focused_source     1.000   1.00   1.000  1 source file changed
    total                     10.00   6.638

  difficulty (easy < 2 <= medium < 4.5 <= hard)
    feature            value weight contrib  detail
    files              0.900   1.00   0.900  9 source+test files changed (full value at 10)
    hunks              0.600   3.00   1.800  6 source code hunks (full value at 10)
    lines              0.240   3.00   0.720  24 source code lines changed (full value at 100)
    cross_file         0.000   2.00   0.000  1 source file with code changes (full value at 5)
    public_api         0.000   1.00   0.000  no public declaration changed
    total                     10.00   3.420
```

The same command on `27be26fa4d` ("Require 100% test coverage", ranked 5th before patches
were measured) now prints:

```text
verdict  rejected (source-cosmetic): source changes touch only comments, blank lines or (outside Python) indentation
```

Its three source hunks only add `# pragma: no cover` comments (`git show --unified=0
27be26fa4d -- tomli/_parser.py`), so its new tests pass on the parent.

### Configuration

Everything the filter, the score and the difficulty use can be set per repository in
`commitminer.toml` (every key is optional; these are the defaults):

```toml
[filter]
max_lines = 400          # source+test lines
max_source_files = 10

[score]
test_lines_cap = 40      # added test lines for the full test_lines_added value
assertions_cap = 5

[score.weights]
small_diff = 3.0
test_lines_added = 2.0
added_assertions = 1.0
linked_reference = 2.0
fix_keyword = 1.0
focused_source = 1.0

[difficulty]
files_cap = 10
hunks_cap = 10
lines_cap = 100
cross_file_cap = 4
medium_at = 2.0          # difficulty 0-10: easy < medium_at <= medium < hard_at <= hard
hard_at = 4.5

[difficulty.weights]
files = 1.0
hunks = 3.0
lines = 3.0
cross_file = 2.0
public_api = 1.0
```

The `[classify]` table adds and disables classifier rules; see
[examples/classify/commitminer.toml](examples/classify/commitminer.toml). When a config is
used, `mine` and `explain` print one line saying what it changes, for example
`config: commitminer.toml: 0 custom rules, 0 built-in rules disabled, 3 scoring settings`.

### Classifying files

`make demo-classify` runs `classify` on [`examples/classify/`](examples/classify/), a tiny
original tree with one file per kind of rule and a `commitminer.toml` that adds a
`fixtures-dir` rule and disables the built-in `tooling-dir` rule (unedited output):

```text
category   rule               path
source     rust-inline-tests  src/lib.rs  [rust-inline-tests]
generated  generated-header   internal/kind/kind_string.go  [generated-header]
generated  minified-content   web/static/bundle.js  [minified]
test       js-test-file       web/src/app.test.ts
vendored   vendored-dir       vendor/github.com/acme/left/left.go
test       fixtures-dir       fixtures/percent.json
source     go-source          tools/cli/main.go
generated  lockfile           Cargo.lock *
docs       docs-file          README.md
* not a file under the root: classified by path only

rules used:
  rust-inline-tests  Rust source with an in-file #[cfg(test)] module: still source, but a commit can change its tests without touching tests/
  generated-header   a comment in the first 30 lines says this file is tool output ('Code generated ... DO NOT EDIT', '@generated', or opening with 'Auto-generated'); one that only mentions generated code does not count
  minified-content   lines averaging 200+ characters: a minified bundle without a .min.js name
  js-test-file       *.test.* and *.spec.* file names (Jest, Vitest, Mocha, Jasmine) next to the code
  vendored-dir       copies of other projects' code (Go and Rust vendor/, npm node_modules/); a change there, tests included, is not this project's fix
  fixtures-dir       this project keeps test inputs and expected outputs in fixtures/
  go-source          Go source
  lockfile           dependency lockfiles (Cargo.lock, yarn.lock, go.sum, uv.lock, ...) are written by the package manager
  docs-file          prose by extension (Markdown, reStructuredText, AsciiDoc, plain text)
```


### Rust and Go histories

Mining live clones of two small public repositories, [dtolnay/semver](https://github.com/dtolnay/semver)
at `280ebcb6ed` (Rust, inline `#[cfg(test)]` modules) and
[spf13/pflag](https://github.com/spf13/pflag) at `c966cfef47` (Go, `*_test.go`). These are
not bundled; the numbers come from `uv run commitminer mine <clone> --rev <sha>` (and with
`--no-content`) on a fresh `git clone`:

| Repository | Before slice 1 (`a2226d2`) | Slice 1 (`1cc6ca9`) | Slice 2 (`0c0112a`) | Now | Now, `--no-content` |
| --- | --- | --- | --- | --- | --- |
| dtolnay/semver, 572 commits | 0 candidates | 50 | 68 | 66 (easy 27, medium 21, hard 18) | 16 |
| spf13/pflag, 285 commits | 0 candidates | 97 | 92 | 92 (easy 41, medium 34, hard 17) | 92 |

In slice 2, semver gained 22 candidates: Rust fixes that edit an existing inline test
without adding a `#[test]` count as changing tests. It lost 4 whose only source edits are
inside test modules (`source-unchanged`). pflag lost 5: two comment or formatting-only
commits (`source-cosmetic`) and three that change 14 to 17 source files (`oversize`).
Since then semver lost the two candidates whose only "source" file was the Cargo build
script: `c41ab5af3a` ("Delete no_track_caller configuration", `build.rs` -7 lines) and
`d2a5e41d4a` ("Backport test suite to rustc <1.46", `build.rs` +6); `build.rs` outside
`src/` is now config, like `setup.py`. 50 of the 66 change no test file at all: their tests
live in the fixed source file. The semver top 5 (the `test` column "0+1" means no test
file, one source file with changed inline tests):

```text
dtolnay/semver: walked 572 commits, 66 candidates (easy 27, medium 21, hard 18), 506 rejected (docs-only 24, no-source 184, source-unchanged 18, source-cosmetic 44, no-test 229, oversize 7)

rank   score         diff  sha         date        lines  src  test  subject
   1    7.41    0.46 easy  5e87530d55  2020-07-22     26    1   0+1  fix tests and formatting to comply with #196
   2    7.40    0.46 easy  55bf7fb619  2016-02-01      7    1   0+1  Fix bug with pre-release parsing
   3    6.94    7.70 hard  0faefa8679  2015-10-29    208    2   0+1  Implement comparison for pre-release tags.
   4    6.81    0.64 easy  2719cb6d47  2016-10-17     19    1   0+1  Bugfix for rust-lang/cargo#3202
   5    6.78    0.43 easy  4f271ff6a3  2015-11-28     16    1   0+1  deal with hyphens correctly
```

`55bf7fb619` with its one-line fix and its new test split apart:

```text
commit   55bf7fb61956504cc9e5019a9e85d3ce83ca246b
base     33087a0fb4f3e40270bd7bf37d156cc19de6bbcb
date     2016-02-01T13:53:25-05:00
subject  Fix bug with pre-release parsing
verdict  candidate: score 7.40 of 10, difficulty 0.46 of 10 (easy)
patch    fingerprint 389bad59ed820ca5 (2 source and test hunks)

  files
    category  rule               added   del hunks  code tests asserts  path
    source    rust-inline-tests      6     1     2     1     5       1  src/version_req.rs

  score (ranks candidates)
    feature            value weight contrib  detail
    small_diff         0.983   3.00   2.947  7 of at most 400 source+test lines changed
    test_lines_added   0.125   2.00   0.250  5 test lines added (full value at 40), in #[cfg(test)] of 1 src file
    added_assertions   0.200   1.00   0.200  1 assertion line added in tests (full value at 5)
    linked_reference   1.000   2.00   2.000  Fixes #73
    fix_keyword        1.000   1.00   1.000  Fix
    focused_source     1.000   1.00   1.000  1 source file changed
    total                     10.00   7.397

  difficulty (easy < 2 <= medium < 4.5 <= hard)
    feature            value weight contrib  detail
    files              0.100   1.00   0.100  1 source+test file changed (full value at 10)
    hunks              0.100   3.00   0.300  1 source code hunk (full value at 10)
    lines              0.020   3.00   0.060  2 source code lines changed (full value at 100)
    cross_file         0.000   2.00   0.000  1 source file with code changes (full value at 5)
    public_api         0.000   1.00   0.000  no public declaration changed
    total                     10.00   0.460
```

`git show 55bf7fb619` confirms it: one changed line in `src/version_req.rs` plus a new
`#[test] fn test_pre()` in the same file's test module. `45323ea034` ("Add test to assert
for issue 88"), ranked 7th before, only adds a test to that module and is now rejected as
`source-unchanged`.

### Dedupe ledger

`make demo-ledger` builds two small repositories with fixed dates
([examples/ledger/build_repos.py](examples/ledger/build_repos.py), an original toy duration
parser), so the shas below are the same on every machine. `upstream` has two fixes on
`main` and a `release` branch that adds license headers to the parser (every line moves
down), cherry-picks fix A cleanly, adds a fix of its own, and cherry-picks fix B with a
conflict on the `UNITS` line resolved by hand. `fork` is an independent copy that
re-indented the code with two spaces, applied fix A, moved the package to `lib/`, applied
fix B there, and added a fix of its own. The demo mines `upstream` (3 candidates), claims
them for `alice`, then mines the release branch and the fork against the ledger (unedited
output from the `ledger add` step on; the dates are the day it ran):

```text
added    #1 demo/durations 536d6c4adf  claimed
added    #2 demo/durations 0287bf15bc  claimed
added    #3 demo/durations a4320cdcff  claimed
3 added, 0 refused: .commitminer/ledger-demo/ledger.sqlite3
demo/durations: walked 5 commits, 4 candidates (easy 4), 1 rejected (source-cosmetic 1)
ledger .commitminer/ledger-demo/ledger.sqlite3: 1 new, 2 duplicate, 1 overlap
  #1 b7894eea9b duplicate: same fix as demo/durations 536d6c4adf (claimed by alice on 2026-09-29)
  #3 352f6038e7 overlap: 2 of 3 hunks shared with demo/durations 0287bf15bc (claimed by alice on 2026-09-29)
  #4 a4320cdcff duplicate: already in the ledger (claimed by alice on 2026-09-29)

rank   score         diff  sha         date        lines  src  test  ledger   subject
   1    7.67    0.92 easy  b7894eea9b  2025-03-02     11    1     1  dup      Reject numbers without a unit (fixes #7)
   2    7.36    0.56 easy  69dbcfede3  2025-03-07      6    1     1  new      Accept upper-case units (fixes #11)
   3    7.34    0.92 easy  352f6038e7  2025-03-04      8    1     1  overlap  Accept days and weeks (fixes #9)
   4    4.36    1.95 easy  a4320cdcff  2025-03-01     25    1     1  dup      Add duration parser
demo/durations-fork: walked 5 commits, 4 candidates (easy 4), 1 rejected (source-unchanged 1)
ledger .commitminer/ledger-demo/ledger.sqlite3: 1 new, 3 duplicate
  #2 e275649a9e duplicate: same fix as demo/durations 536d6c4adf (claimed by alice on 2026-09-29)
  #3 6dbfaa8121 duplicate: same fix as demo/durations a4320cdcff (claimed by alice on 2026-09-29)
  #4 287f42b780 duplicate: same fix as demo/durations 0287bf15bc (claimed by alice on 2026-09-29)

rank   score         diff  sha         date        lines  src  test  ledger   subject
   1    7.36    0.56 easy  92ee5dce6f  2025-03-14      6    1     1  new      Allow spaces between parts (fixes #3)
   2    4.67    0.92 easy  e275649a9e  2025-03-11     11    1     1  dup      Reject numbers without a unit
   3    4.36    1.95 easy  6dbfaa8121  2025-03-10     25    1     1  dup      Import durations with two-space indentation
   4    4.34    0.92 easy  287f42b780  2025-03-13      8    1     1  dup      Accept days and weeks
.commitminer/ledger-demo/ledger.sqlite3: 3 entries (schema version 1)
first seen  status    owner       repo                sha         hunks  fingerprint       subject
2026-09-29  claimed   alice       demo/durations      536d6c4adf      3  d3c6af7bc86c6f62  Reject numbers without a unit (fixes #7)
2026-09-29  claimed   alice       demo/durations      0287bf15bc      3  c282f61bc585bf9b  Accept days and weeks (fixes #9)
2026-09-29  claimed   alice       demo/durations      a4320cdcff      2  7842e726a4b52616  Add duration parser
```

The cherry-pick of fix A and the fork's re-indented and moved copies are duplicates; the
cherry-pick with a resolved conflict keeps 2 of its 3 hunks and is an overlap; the
maintenance branch's and the fork's own fixes are new. The release branch's shared root
commit is "already in the ledger", and the fork's rename-only commit is rejected
(`source-unchanged`) before the ledger is asked. `ledger add` on the same file again refuses
all three and exits with 1.

On real histories the in-run check finds two pairs among 202 candidates (`mine <clone>
--ledger <empty file>`): semver's `dcfb5efdea` (`Revert "Revert "Implement comparison for
pre-release tags.""`) shares 45 of its 46 hunks with `0faefa8679`, the change it re-lands;
pflag's `13e924deb5` ("fix bug of string_slice with square brackets") shares 1 of 2 hunks
with `b027180f68` ("Fix square bracket handling in string_array"): the same one-line fix
(`sval = sval[1 : len(sval)-1]`) in two files, with different tests. Eleven more pairs
(one in tomli, three in semver, seven in pflag) share exactly one hunk, at most a quarter of
the smaller fix, and stay new under the default `--min-overlap 0.5`. No two candidates have
the same patch hash, and every candidate of the three histories has a fingerprint.

### Merged pull requests

`commitminer prs OWNER/REPO` reads `GET /repos/{owner}/{repo}/pulls?state=closed&sort=updated`
page by page, keeps the first `--limit` merged pull requests, and for each reads
`pulls/{number}/files` (first, 100 per request; a pull request with more than
`--max-files` changed files is skipped after at most 3 requests) and `pulls/{number}/commits`.
Each one becomes a commit: the merge commit sha, GitHub's base sha as its parent (the base
commit a task starts from), the merge date, the title and description as its message. The
same filters, score, difficulty, fingerprint and ledger apply. Pull-request metadata feeds
two score features: `linked_reference` is 1.0 when the description or a commit message
closes an issue (`Fixes #12`, `owner/repo#12`, an issue URL) and 0.5 for the pull request
itself; `fix_keyword` is also set by a `bug`, `fix`, `regression` or `crash` label
(`type: bug`, `C-bug`, `kind/regression`). Without `GITHUB_TOKEN` GitHub allows 60
requests an hour, which is about 29 pull requests (1 + 2 requests each); with it, 5000.

Output of `make demo-prs` (unedited from the first command on; it replays the 52 recorded
responses in `examples/tomli/prs/`, so the rate-limit counters are the recorded ones):

```text
github: 52 requests (0 answered 304 from the cache), 0 retries, waited 0 s; rate limit 4759 of 5000 left, resets 2026-09-30 01:25:39 UTC (replayed from examples/tomli/prs)
hukkin/tomli: read 24 merged pull requests (6 closed without merging passed over; skipped #278: more than 300 changed files)
hukkin/tomli: walked 24 pull requests, 6 candidates (easy 4, medium 2), 18 rejected (docs-only 1, no-source 11, source-cosmetic 1, no-test 5)

rank   score         diff  pull        date        lines  src  test  subject
   1    6.55  2.10 medium  #200        2026-01-10     60    1     5  Allow newlines and trailing comma in inli...
   2    6.06    0.53 easy  #295        2026-04-10     25    1     1  Use Python 3.15 lazy import
   3    5.84    0.95 easy  #286        2026-03-25     28    1     1  Limit number of parts of a key
   4    5.37    0.76 easy  #202        2026-01-10     17    1     3  Add \xHH Unicode escape code to basic str...
   5    5.12    0.63 easy  #201        2025-12-21      4    1     2  Add shorthand for escape character
   6    5.02  2.67 medium  #203        2026-01-10     44    1     5  Make seconds optional in Date-Time and Time
```

followed by the contribution tables of #200. In `out/tomli-prs.jsonl` each candidate
carries a `pull_request` object; the first one's, with its seven commit shas cut to two:

```json
{
  "base_ref": "master",
  "base_sha": "38297f82cd0ef067f1afd2ffb8dfa73b65c398da",
  "commits": ["aae9af0b62e4c289fc53c73d6e593fd23a9b7813", "...", "4133dde4238f0ab6e3edd166170f824751ca1d71"],
  "head_sha": "4133dde4238f0ab6e3edd166170f824751ca1d71",
  "labels": [],
  "linked_issues": [],
  "merge_commit_sha": "2a2aa62f1bc71b89b74d41dd2ab67b5dd24bc129",
  "merged_at": "2026-01-10T12:41:08+00:00",
  "number": 200,
  "title": "Allow newlines and trailing comma in inline tables",
  "url": "https://github.com/hukkin/tomli/pull/200"
}
```

The second run uses the same ETag cache:

```text
github: 52 requests (52 answered 304 from the cache), 0 retries, waited 0 s; rate limit 4759 of 5000 left, resets 2026-09-30 01:25:39 UTC (replayed from examples/tomli/prs)
```

Then the recorded commit history is mined and its 44 candidates are claimed in a ledger,
and the pull requests are checked against it:

```text
44 added, 0 refused: .commitminer/prs-demo/ledger.sqlite3
ledger .commitminer/prs-demo/ledger.sqlite3: 6 duplicate
  #1 #200 duplicate: already in the ledger (claimed by demo on 2026-09-30)
  #2 #295 duplicate: already in the ledger (claimed by demo on 2026-09-30)
  #3 #286 duplicate: already in the ledger (claimed by demo on 2026-09-30)
  #4 #202 duplicate: already in the ledger (claimed by demo on 2026-09-30)
  #5 #201 duplicate: already in the ledger (claimed by demo on 2026-09-30)
  #6 #203 duplicate: already in the ledger (claimed by demo on 2026-09-30)
```

tomli squash-merges, so each pull request's merge commit is one of the recorded commits,
and its patch, rebuilt from GitHub's three-line-context patches, has the same fingerprint
as `git log --unified=0` gives that commit (`tests/test_cli_prs.py` checks all six, and
that their scores are equal too). Three of the 24 pull requests close an issue
(#272 closes #271, #276 #253, #280 #273); two change only the CI workflow (`no-source`)
and #280 is a version bump without tests (`no-test`). #278 ("Update external test data") changes 1397 files (`gh api repos/hukkin/tomli/pulls/278 --jq .changed_files`) and is skipped.

Live, with a token (`GITHUB_TOKEN=$(gh auth token) uv run commitminer prs hukkin/tomli
--limit 25 --cache-dir <dir>`, run twice): the first run sent 52 requests in 24.65 s and
left 4700 of 5000; the second got 52 answers of 304 from the cache in 23.79 s and still
left 4700. Without a token, `--limit 3 --no-cache` sent 7 requests and left 53 of 60, and
the cache does not stretch that budget: GitHub only exempts 304 answers to authenticated
requests. Three runs of `env -u GITHUB_TOKEN -u GH_TOKEN uv run commitminer prs
hukkin/tomli --limit 1 --cache-dir <dir> --top 0 --explain 0 --max-wait 5` sent 3 requests
each and left 56, then 53 (3 answered 304), then 50 of 60; three more runs with a fresh
cache left 46, 43 and 40.

### Export schema and reports

`mine --out` and `prs --out` write one JSON object per line. The first line is the run
record; every other line is one candidate, best first. Trimmed from
`out/tomli-candidates.jsonl` after `make demo-report` (the rejections list has 268
entries, the settings every limit and weight, and a candidate also carries its files with
their patch measurements, both feature breakdowns and the public API it touches):

```json
{"kind": "run", "schema_version": 5, "commitminer": "0.1.0", "repo": "hukkin/tomli",
 "url": "https://github.com/hukkin/tomli", "source": "history", "unit": "commits",
 "walked": 312, "candidates": 44, "bands": {"easy": 15, "medium": 17, "hard": 12},
 "rejected": {"docs-only": 42, "no-source": 100, "source-unchanged": 1, "source-cosmetic": 6, "no-test": 115, "oversize": 4},
 "rejections": [{"sha": "5a77b12a7a9f052ce5a20c335d2825658f6aea52", "date": "2026-04-14T11:34:49+02:00", "subject": "Use frozendict on Python 3.15", "reason": "no-test", "pull_request": null}, "..."],
 "ledger": null, "exported": 44,
 "settings": {"max_lines": 400, "max_source_files": 10, "test_lines_cap": 40, "weights": {"small_diff": 3.0, "...": "..."}, "...": "..."}}
{"kind": "candidate", "schema_version": 5, "rank": 1, "repo": "hukkin/tomli",
 "repo_url": "https://github.com/hukkin/tomli",
 "sha": "5ab9ec926d9dc1ef79e66215edd51285371fe8a0", "base": "37a543b74bb1633478aea9f3a6a450a550bdeb63",
 "date": "2021-05-28T23:10:06+02:00", "subject": "NEW: Allow float parse func customisation (#2)",
 "score": 6.885, "source_files": ["tomli/_parser.py"], "test_files": ["tests/test_misc.py"],
 "fail_to_pass": ["tests/test_misc.py::test_deepcopy", "tests/test_misc.py::test_parse_float"],
 "fingerprint": {"version": 2, "patch": "f10c534e4424fd9b", "hunks": ["2238f1a042c06dea", "281157c378695ed6", "..."]},
 "ledger": null, "pull_request": null, "...": "..."}
```

A downstream builder reads `repo_url`, `base` (the commit a task starts from), `sha` (the
fix), `source_files`, `test_files` and `fail_to_pass`, then verifies the flip by running
those tests on both commits. `commitminer schema` prints the JSON Schema
(draft 2020-12, `additionalProperties: false` on every record, so a field the export
starts writing without a schema change fails the tests); the ledger commands read schema
3 to 5 and skip the run record. The fail-to-pass ids are a guess from the diff: in the
demo's top candidate, `5ab9ec926d` adds `test_parse_float` and changes lines inside
`test_deepcopy` (`git show --unified=0 5ab9ec926d -- tests/test_misc.py`); tomli keeps
those tests as module-level functions, and its `unittest` classes give ids such as
`tests/test_misc.py::TestMiscellaneous::test_incorrect_load` (#3).

`commitminer report` renders the export. The first 22 lines of `out/tomli-report.md`, as
`make demo-report` prints them (the HTML report has the same content in one page with
collapsible sections):

```markdown
# CommitMiner report: hukkin/tomli

Source: a recorded history of [https://github.com/hukkin/tomli](https://github.com/hukkin/tomli), 312 commits walked. CommitMiner 0.1.0, export schema version 5.

## Funnel

| step | count | remaining |
| --- | ---: | ---: |
| walked commits | 312 | 312 |
| rejected: docs-only (every changed file is documentation) | 42 | 270 |
| rejected: no-source (no source file changed (only tests, config or other files)) | 100 | 170 |
| rejected: source-unchanged (no source line changed outside inline tests (renames, mode changes, binary files)) | 1 | 169 |
| rejected: source-cosmetic (source changes touch only comments, blank lines or (outside Python) indentation) | 6 | 163 |
| rejected: no-test (no test file changed and no inline tests were added) | 115 | 48 |
| rejected: oversize (more source+test lines or source files than the limits allow) | 4 | 44 |
| candidates | 44 | 44 |
| exported | 44 | 44 |

Difficulty bands: easy 15, medium 17, hard 12.

## Ranked candidates
```

The ranked table that follows has a column per score feature (`small_diff`,
`test_lines_added`, `added_assertions`, `linked_reference`, `fix_keyword`,
`focused_source`), the lines, files and test-id counts, the ledger status when the export
was mined with `--ledger`, and the subject; then one section per candidate, the rejected
commits grouped by reason (collapsed in HTML), and the settings. The golden reports of the
ledger demo's fork are in [tests/golden/report-fork.md](tests/golden/report-fork.md) and
[report-fork.html](tests/golden/report-fork.html).

Docker (the image contains the recorded history and the sample tree, so this runs offline):

```sh
make docker   # build, run the demo inside the image, prune this project's dangling images
docker run --rm commitminer:local explain 948211d852 --history examples/tomli/history.jsonl.gz
docker run --rm --network none commitminer:local prs hukkin/tomli --limit 25 \
  --replay examples/tomli/prs
docker run --rm --entrypoint sh commitminer:local -c 'commitminer mine --history \
  examples/tomli/history.jsonl.gz --out /tmp/c.jsonl && commitminer report /tmp/c.jsonl --top 3'
# Mine a clone on the host; -u keeps git's ownership check happy on Linux.
docker run --rm -u "$(id -u):$(id -g)" -v "$PWD:/repo:ro" commitminer:local mine /repo
```

## Architecture

```mermaid
flowchart LR
    clone[local clone] -->|"git log -z --numstat -p --unified=0"| walk[gitlog: streamed walk]
    walk -->|"git cat-file --batch"| signals[signals and Rust test modules]
    walk --> patch[patch: hunks, code, asserts, API]
    signals --> commits
    patch --> commits[Commit + FileChange + PatchStats]
    commits -->|commitminer record| rec[(history.jsonl.gz)]
    api[GitHub REST API] -->|"httpx: ETag cache, rate limits, retries"| client[github: client]
    fixtures[(recorded fixtures)] -->|--replay| client
    client -->|--record| fixtures
    client --> pulls[pulls: merged PRs, files, commits, linked issues]
    pulls -->|"U3 patches split to U0 hunks"| patch
    pulls --> commits
    rec -->|history.read_history| commits
    toml[(commitminer.toml)] --> classify
    toml --> settings[settings: limits, caps, weights, bands]
    commits --> classify[classify: ordered rule table]
    classify --> stats[stats: per-category lines, hunks, asserts, API]
    stats --> filters[filters: reason codes]
    settings --> filters
    filters -->|rejected| funnel[summary funnel]
    filters -->|candidates| scoring[scoring: score + difficulty band]
    settings --> scoring
    patch -->|hunk hashes| commits
    stats --> fingerprint[fingerprint: source + test hunks]
    fingerprint --> scoring
    ledgerdb[(ledger.sqlite3)] --> ledger[ledger: duplicate / overlap / new]
    scoring --> ledger
    signals --> testids[testids: test functions by line range]
    testids --> patch
    ledger --> export
    scoring --> export[export: run record + candidates JSONL, table, breakdowns]
    export -->|validated by| schema[(export-v5.schema.json)]
    export -->|commitminer report| report[report: Markdown / HTML]
    scoring --> explain[explain: one commit]
```

| Module | Role |
| --- | --- |
| `models.py` | frozen dataclasses `Commit`, `PullRequest`, `FileChange` and `PatchStats` |
| `gitlog.py` | builds the git command and its environment, streams and parses the NUL-separated output, matches patches to numstat; reads file contents for signals with `git cat-file --batch` |
| `patch.py` | `--unified=0` patch parser (and a splitter for patches with context), per-language comment, assertion, declaration and test-definition rules, a C-like lexer for block comments and Rust test modules, the tests a patch touches |
| `testids.py` | test functions of a file version with their line ranges, named as pytest, cargo, go test, JUnit and Jest take them |
| `github.py` | REST client: ETag cache on disk, rate limits, retries and backoff with an injectable clock, pagination |
| `fixtures.py` | httpx transports that record responses as fixture files and replay them offline |
| `pulls.py` | merged pull requests with their files and commits as `Commit` records, linked issues |
| `history.py` | writes and validates recorded histories (JSONL, optional reproducible gzip) |
| `languages.py` | the six languages and extension detection |
| `signals.py` | content-signal detectors over file bytes |
| `globs.py` | glob syntax, pattern checks, linear-time component and path matching |
| `classify.py` | the ordered rule table, Java package-directory handling and `classify(path, rules, signals)` |
| `config.py` | `commitminer.toml` loading and validation |
| `settings.py` | limits, caps, weights and band thresholds with their defaults |
| `stats.py` | per-category measurements of one commit |
| `filters.py` | hard filters and reason codes |
| `scoring.py` | score and difficulty features, ranking |
| `fingerprint.py` | hunk hashes (whitespace, path and position insensitive) and commit fingerprints |
| `ledger.py` | the SQLite ledger: schema and migrations, verdicts, atomic claims, reading exported candidates |
| `export.py` | the run record and candidate JSONL export, the JSON Schema accessor, the terminal renderers |
| `schemas/` | `export-v5.schema.json`, the committed JSON Schema of the export records |
| `report.py` | reads an export and renders the Markdown and self-contained HTML reports |
| `explain.py` | the `explain` output, text and JSON |
| `ruletable.py` | `classify` and `rules` command output, `docs/rules.md` |
| `cli.py` | Typer commands `mine`, `prs`, `report`, `schema`, `explain`, `record`, `classify`, `rules`, `ledger add/check/list`, `version` |

## Measured

| What | Command | Result |
| --- | --- | --- |
| Tests and coverage | `make cov` | 941 passed, 100.00% line and branch coverage (gate 90%) |
| Types | `make typecheck` | `mypy --strict`: no issues in 26 source files |
| Classifier table | `commitminer rules --markdown` | 35 rules, each with positive and negative examples in `tests/test_classify.py` |
| Demo funnel | `make demo` | 312 commits walked, 44 candidates (easy 15, medium 17, hard 12), 268 rejected |
| Live walk of the tomli clone | `/usr/bin/time -p uv run commitminer mine <tomli clone> --top 0 --explain 0` | 0.40 to 0.44 s with content signals, 0.35 to 0.42 s with `--no-content` (3 runs each; 0.39 to 0.42 s and 0.37 s before slice 4) |
| Live walk of the semver clone | same on dtolnay/semver (572 commits) | 0.67 s with content signals, 0.27 to 0.28 s with `--no-content` (3 runs each; 0.63 to 0.66 s and 0.36 s before slice 4) |
| Live walk of the pflag clone | same on spf13/pflag (285 commits) | 0.30 to 0.31 s (3 runs) |
| Live walk of the serde clone | same on serde-rs/serde at `6693a89c` (3542 commits; aborted on a symlink type change before slice 4) | 8.47 to 9.48 s (3 runs): 484 candidates |
| Replay of the recording | same with `--history examples/tomli/history.jsonl.gz` | 0.19 to 0.22 s (3 runs, with test ids; 0.18 to 0.20 s before them; httpx and the GitHub client are imported only by `prs`) |
| Report | `/usr/bin/time -p uv run commitminer report out/tomli-candidates.jsonl --out r.html` | 0.08 to 0.11 s (3 runs); `ls -l out/`: 102355 bytes of Markdown, 151400 of HTML for 44 candidates and 268 rejections |
| Likely fail-to-pass ids | `make demo-report`, then count `fail_to_pass` in the export | 30 of 44 tomli candidates (the other 14 change only `.toml`/`.json` test data); 2 of the 6 pull-request candidates (no file contents there) |
| Replay of the pull requests | `uv run commitminer prs hukkin/tomli --limit 25 --replay examples/tomli/prs --top 0 --explain 0` | 0.12 to 0.14 s (3 runs), 52 requests answered from 52 fixture files |
| Live pull requests | same without `--replay`, with `GITHUB_TOKEN` and a fresh `--cache-dir`, twice | 24.65 s, 52 requests, rate limit 4700 of 5000 left; again: 23.79 s, 52 answered 304, still 4700 left |
| Fixture size | `du -sh examples/tomli/prs`; `ls -lS` | 396 KB in 52 files, the largest 82378 bytes (the first page of 100 closed pull requests, about 1.5 MB before trimming) |
| One explanation | `uv run commitminer explain 55bf7fb619 --repo <semver clone>` | 0.10 s (3 runs) |
| A minified line | `patch.code_text` on `"var a=b/c,d=e/f;x.y(z)/2;" * 8000` (200000 characters, 24000 slashes), timed with `time.perf_counter` | 0.05 s; 50.75 s before the regex check read only a bounded window before each `/` |
| Mining against a ledger | semver walk with `--ledger` holding its 66 candidates | 0.66 to 0.67 s (3 runs): 65 duplicate, 1 overlap |
| Checking an export | `uv run commitminer ledger check <that ledger> <semver export>` | 0.08 s (3 runs) |
| Concurrent claims | `tests/test_ledger.py`: 8 threads, 8 connections, one fix | exactly 1 added, 7 refused |
| Live vs replay | `mine <clone> --repo-name hukkin/tomli --out a.jsonl`, `make demo`, `cmp` | identical |
| Recording size | `ls -l examples/tomli/history.jsonl.gz` | 126060 bytes (1046553 uncompressed) with version 2 hunk hashes and test ids; 125342 before test ids, 125311 with version 1 hashes, 67715 without hashes, 63697 before patch measurements |
| Recording integrity | `make verify-recording` (also in CI) | byte-identical to a fresh recording from GitHub |
| Image size | `docker image inspect commitminer:local --format '{{.Size}}'` | 112977638 bytes (112840949 before the schema and the report; 110897309 before httpx) |

## Design decisions

- **One git call, NUL-separated and streamed.** The walker reads commit headers, numstat
  and the patch from a single `git log -z` stream, one commit at a time, so memory stays
  flat. Commit messages cannot contain NUL, so each header field is one token; numstat
  entries are self-delimiting. Patch text can contain NUL (git only checks a file's first
  8000 bytes before calling it binary, a `diff` attribute forces text, and a hunk header
  copies a file line as function context), but it always ends with a newline and a NUL
  inside it never follows one, so the parser rejoins a commit's patch tokens until one
  ends with a newline. File blocks come in numstat order; a type change (a file that
  became a symlink or a submodule), which git prints as a deletion block and a creation
  block under one header, is merged back into one. Each block is checked against its
  numstat counts, so a misread is an error, not a wrong number. `--end-of-options` stops a
  revision from being read as an option.
- **Fixed git environment.** `LC_ALL=C`, no system or global config, no pager, and
  command-line overrides for `core.fsmonitor`, `log.showSignature`, `log.showRoot`,
  `color.ui`, the diff algorithm, inter-hunk context, indent heuristic, submodule format
  (`--submodule=short`, `--ignore-submodules=none`) and file order (`-O/dev/null`). The
  caller's `GIT_DIFF_OPTS`, `GIT_EXTERNAL_DIFF` and `GIT_CONFIG_*` variables and the
  repository-selecting ones (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE` and so on, set
  inside git hooks) never reach git. Tests set `diff.interHunkContext`, `diff.algorithm`,
  `diff.noprefix`, `diff.context`, `diff.submodule`, `diff.ignoreSubmodules`,
  `GIT_DIFF_OPTS`, `GIT_DIR` and `GIT_CONFIG_*` and get the same result. Settings that
  are not pinned (such as `diff.renameLimit`) can still change which renames are found,
  but not how the output parses. UTC dates are normalised to `+00:00` because git
  versions differ on printing `Z`.
- **Hunks read by count.** With `--unified=0` every hunk is deleted lines then added lines,
  and the header gives both counts, so a content line such as `+++ x` is never mistaken for
  a header.
- **Measurements at walk time, text never stored.** Patch measurements depend only on the
  patch, the language and (for Rust test modules) the file contents, not on the rule table
  or weights, so they are computed while walking and stored in the recording in a compact
  form (fields equal to their defaults are left out). A `commitminer.toml` change needs no
  re-recording; a change to the measurement rules does, and CI's byte-for-byte
  re-recording check catches a stale bundled recording.
- **Cosmetic means provably cosmetic.** A hunk counts as code unless its deleted and added
  code lines are the same sequence after dropping comments and blank lines and, outside
  Python, surrounding whitespace. Whitespace inside a line is kept: semver's `5e87530d55`
  changes `"{} {}"` to `"{}{}"`, a real behaviour change. Hunks are compared one at a time,
  so moving a statement to another place still counts as a change. A `*` line is judged
  by where it sits: when a hunk has one, the file version it belongs to (read anyway for
  content signals, or the parent version for deleted lines) is lexed for block comments.
  Without the contents (`--no-content`, or a file cut at the 1 MiB read limit) only a
  comment opened in the same hunk, a bare `*` or a leading `*/` makes it a comment.
- **Record once, replay everywhere.** Demos, the Docker image and CI mine a recorded
  history, so they are offline and reproducible; recordings have sorted keys and no
  timestamps, and gzip uses `mtime=0`. Recordings keep commit messages but not the author
  and committer fields.
- **Hard filters first, soft score second.** Commits that cannot become a fail-to-pass task
  are rejected with a reason code instead of getting a low score, so the funnel is
  auditable, and `explain` names the reason for any single commit.
- **Two linear models, both explainable.** The score (what to look at first) and the
  difficulty (how much work the fix is) are separate sums of `weight * value` with values
  in `[0, 1]` and default weights that sum to 10. Difficulty does not change the ranking:
  a task author may want hard tasks, easy ones, or a mix. No LLM and no learned model:
  every number can be recomputed by hand from the feature details.
- **Difficulty measures the fix, not the tests.** Hunks, lines, cross-file spread and public
  API are counted on source code only; the tests are what grades the task.
- **Size counts source and test lines only.** Changelog, docs and CI churn does not make a
  task harder, so it does not count against `--max-lines`.
- **One ordered rule table, first match wins.** Vendored copies come first (their tests are
  not this project's), then test directories (so golden files under `testdata/` stay test
  data even when they carry a generated header), generated files, test file names,
  configuration, documentation, tooling, source, and last prose names such as `LICENSE`
  (so a module named `license.py` stays source). Every rule carries its rationale, and
  `docs/rules.md` is generated from the table and checked by a test.
- **Java packages are not layout.** Below a Maven or Gradle source root (`src/<set>/java/`)
  directories are package names, so directory rules skip them: `com/shop/vendor/` is not
  vendored and `org/springframework/test/` is not a test tree. Test class names
  (`*Test.java`, `Test*.java`) do not count under `src/main/`, which Maven compiles as main
  code (junit5's own `Test.java` is source), while an `examples/` module above the source
  root is still tooling.
- **Rust inline tests by measuring, not guessing.** A Rust file with a `#[cfg(test)]`
  module is a test change when its `#[test]` count grew or lines inside the module were
  added; lines inside the module are test lines, the rest are code. A commit whose only
  source edits are inside test modules is rejected (`source-unchanged`).
- **Fingerprints ignore what copies change, not what fixes change.** Leading and trailing
  whitespace is dropped and inner runs are collapsed (re-indentation, tabs, realigned
  columns), but a space where there was none still counts. Removing all whitespace was
  tried first: semver's top candidate `5e87530d55` changes `"{} {}"` to `"{}{}"`, and it
  was left with nothing to hash. A review then found the same gap one step further: a fix
  of `f"{a}  {b}"` to `f"{a} {b}"` collapsed to identical lines and had no fingerprint, so
  `--new-only` silently dropped it and `ledger add` refused it for ever. Such a hunk is
  now hashed a second way, with inner whitespace kept (fingerprint version 2; the version
  is stored in ledgers and exports, and both refuse the other version). Only source and
  test hunks count, because a fork or a backport usually has its own changelog and CI
  edits.
- **Hunk hashes are measurements.** Like the other patch measurements they are computed
  while walking and stored in recordings (64 bits per hunk; the tomli recording grew from
  67715 to 125311 bytes, 125342 with fingerprint version 2), so replay needs no patch
  text. Their absence in an old recording means "unknown", never "no hunks".
- **Check in memory, claim in one transaction.** `mine --ledger` and `ledger check` copy the
  ledger into memory with SQLite's backup API and add each checked candidate to the copy,
  so duplicates inside one run and against the ledger are found by the same query and the
  shared file is never written. `ledger add` checks and inserts inside `BEGIN IMMEDIATE`,
  and `UNIQUE (fingerprint)` and `UNIQUE (repo, sha)` refuse a second claim even from a
  writer that skipped the check.
- **A pull request is a commit with metadata.** Rather than a second pipeline, each merged
  pull request becomes a `Commit` (merge commit sha, GitHub's base sha as parent, merge
  date, title and description) with a `PullRequest` attached, so filters, score,
  difficulty, fingerprints, ledger, export and tables are shared. The base is GitHub's
  `base.sha`, the usual starting point for a task built from a pull request.
- **Same hunks from both sources.** GitHub sends per-file patches with three context lines;
  splitting them at context lines and numbering empty sides as git does gives the hunks of
  `git diff --unified=0` (a test compares both on a real repository), so a pull request
  and the commit it became have the same fingerprint.
- **The HTTP layer is swappable, the client is not.** Recording and replay are `httpx`
  transports, so cache, rate-limit, retry and pagination code runs unchanged on live,
  recorded and replayed traffic. A recording run hands the client exactly what a replay
  will serve (kept headers, trimmed body). Replay answers `304` when `If-None-Match` equals
  a recorded ETag, which lets the cache be tested offline. Fixtures are keyed by method,
  path and sorted query, one readable JSON file per request, with scripted sequences
  (`403` then `200`) for retry tests.
- **Spend the rate limit on candidates.** Files are read before commits and at most
  `--max-files` of them, conditional requests are free with a token (and still served
  from the cache without one), a pull request costs 1 + 2 requests, and waiting is bounded
  by `--max-wait` so an unauthenticated run fails with a hint to set `GITHUB_TOKEN`
  instead of sleeping for an hour.
- **Test ids are line ranges, measured once.** The walker already reads every changed
  code file for signals, so the test functions of the new version are found there (one
  regex pass per line, brace matching with the existing lexer) and a hunk names every
  function its new-side lines overlap; a deletion-only hunk is placed by the line git
  numbers it with. The names are stored per file in recordings like the other
  measurements, without the path, so a rename does not change them; the export joins
  `path::name`. Trailing blank and comment lines are left out of a Python function's
  range, so an insertion after it is not attributed to it. Without contents only added
  definitions are named, which is what pull requests get.
- **A run record, not a sidecar file.** The report needs the funnel and the rejected
  commits, which candidates alone cannot give. The export's first line carries them (and
  the settings, so a file says how it was made); readers filter on `kind`, the ledger
  reader skips it, and one file still travels as one unit. Reports are built from the
  export, never from a live run, so a report can be regenerated later and the same file
  feeds the builder and the reviewer.
- **The schema forbids unknown fields.** Every record type in the JSON Schema has
  `additionalProperties: false` and a complete `required` list, so adding a field to the
  export without bumping the schema fails `tests/test_schema.py` on the tomli, pull-request
  and ledger exports, and CI validates the demo export with the same schema.
- **One document, two renderers.** The report is built once as headings, paragraphs,
  tables and collapsible sections, then written as Markdown or HTML, so both formats
  always say the same thing. Every string from the export is escaped for the format
  (entities in HTML, backslashes before Markdown punctuation), links are made only for
  http(s) URLs (a `git@host:path` remote is turned into one; `javascript:` or a local path
  is printed as text), and the HTML has inline CSS and nothing to load.
- **Fresh repository, not a fork.** PyDriller (Apache-2.0) was considered; CommitMiner
  needs only a narrow, typed parse of `git log`, and calling the git CLI keeps the
  dependency set small (Typer, and httpx for the GitHub API) and `mypy --strict` clean.
  See [PLAN.md](PLAN.md).

## Known issues

- **Build changes still look like fixes.** #5 in the demo, `149547d2ec` ("Create binary
  wheels with mypyc"), is mostly a build change (`git show --numstat 149547d2ec`: 103 of
  its 219 added lines are in the CI workflow); its source edits are real code, so no filter
  drops it.
- **Line-based heuristics, no syntax tree.** Python docstrings count as code; a line inside
  a `/* ... */` comment that does not start with `*` counts as code; a trailing comment
  after a string that opens a multi-line literal is kept; with `--no-content` a Javadoc
  line edited in the middle of a comment counts as code. All of these err towards "code",
  so they can keep a cosmetic commit, not drop a real one. The block-comment lexer does
  not follow `${...}` inside JavaScript template literals, and tells a regex literal from
  a division by the character before the `/`.
- **Public API by naming convention.** Python counts only top-level `def`/`class` without a
  leading underscore, in any module: tomli's `_parser.py` is private by name but its
  `loads` is re-exported, and its `DEPRECATED_DEFAULT` class counts too. Methods, Go struct
  fields, Java interface methods without `public`, and re-exports are not followed. At most
  32 names are kept per file.
- **Assertion patterns are framework lists.** Custom helpers count only when named
  `assert_*` (Python, Rust); others (Go helpers such as `checkEqual(t, ...)`, Jest
  matchers without `expect(`) do not. tomli's data-driven tests add TOML/JSON cases with no
  assertion lines, so they get 0 for `added_assertions`.
- **Band thresholds are judgment calls.** The defaults (easy below 2, hard from 4.5) were
  chosen so a one-file fix of up to 3 hunks and 20 code lines with its test is easy (1.7)
  and a public-API change over three source files, 5 hunks and 40 lines is hard (5.1),
  then checked against the spread on tomli,
  semver and pflag (easy 15/28/41, medium 17/22/34, hard 12/18/17). They are not calibrated
  against how often agents solve such tasks. Hunk-heavy commits rank as hard even when each
  hunk is one line (`5ab9ec926d` threads one parameter through 10 hunks).
- **Rust test modules need contents.** With `--no-content`, or in a recording made with it,
  lines inside `#[cfg(test)]` modules count as source code, so semver drops from 66 to 16
  candidates. Detection looks at the first 1 MiB of a file.
- **Content limits.** Paths containing a newline cannot be requested from
  `git cat-file --batch` and get no signals. Kotlin, C/C++ and other languages have no
  rules and fall back to `other`; their patches are measured without comment rules.
- **Test data counts as test lines.** tomli keeps its cases as `.toml`/`.json` files under
  `tests/`; they count toward `test_lines_added`, which suits tomli but may overrate
  fixture-heavy commits elsewhere.
- **Recordings go stale when measurement rules change.** A recording stores measurements,
  not patch text; after changing `patch.py`, `fingerprint.py` or `testids.py`, re-record
  (CI's re-recording check fails until the bundled one is refreshed). A recording does
  not say which fingerprint version its hunk hashes have.
- **English keywords only** for `fix_keyword` and closing references.
- **Directory rules match any path component.** A Go or Python package directory named
  `tools`, `scripts`, `examples`, `docs` or `test` is classified by that directory rule
  (Java package directories below `src/<set>/java/` are skipped). Override with
  `commitminer.toml`.
- **Docker and file ownership.** git refuses a repository owned by another user, and the
  walker deliberately ignores global config (so `safe.directory` cannot be set there);
  mining a bind-mounted clone on Linux needs `-u "$(id -u):$(id -g)"`.
- **Fingerprints are textual.** The same fix written differently (other variable names, a
  formatter that adds spaces around operators) gets other hunk hashes; hunk boundaries come
  from git's diff, so a cherry-pick onto code that changed around the fix can split or
  merge hunks and share fewer of them. Hunks that change only indentation, trailing
  whitespace or blank lines are left out, so a Python fix that only re-indents a block
  (moving statements into an `if`, a real change there) has no fingerprint and is
  reported as `unknown` by the ledger. A comment realigned inside a verbose regex
  (tomli's `81c4eec444`) counts as a changed string and gets a hash.
- **Overlap counts hunks, not lines.** A one-line hunk weighs as much as a 40-line one:
  pflag's `13e924deb5` and `b027180f68` apply the same one-line fix to two files with
  different tests and are reported as overlapping (1 of 2 hunks) at the default 0.5; raise
  `--min-overlap` to ignore such pairs.
- **The ledger's repository is a label.** `--repo-name` (or the directory name) is stored,
  not a URL, so one repository mined under two labels counts as two (its fixes still collide
  by fingerprint). Statuses are `proposed` and `claimed`; there is no command yet to change
  a status or release a claim.
- **SQLite locking on network drives.** Concurrent claims rely on SQLite's file locks,
  which some network filesystems do not implement correctly; the concurrency test runs on a
  local disk.
- **Pull-request files are classified by path only.** File contents are not fetched, so
  content signals (generated headers, minified JavaScript) and Rust `#[cfg(test)]`
  modules are not seen: a Rust pull request whose tests are inline is rejected as
  `no-test`, as with `--no-content`. A `*` line is judged without the file (see the
  comment rule above).
- **Patches GitHub leaves out are unmeasured.** GitHub omits the patch of very large or
  binary files; such a file keeps its line counts but has no patch measurements, so the
  commit's features read "unknown: no patch data" and it has no fingerprint when that file
  is source or test.
- **`--limit` follows GitHub's update order.** The pull requests read are the most
  recently updated closed ones that were merged, not the most recently merged; a comment
  on an old pull request brings it forward. Requests are sequential (about 0.47 s each in
  the live run above).
- **Linked issues are keyword links only.** Issues linked in GitHub's "Development"
  sidebar without a closing keyword are not found (the REST API does not list them), and
  a closing keyword in a pull-request title is not read (GitHub documents the description
  and commit messages as the places that link).
- **Base sha is GitHub's.** `base.sha` is the base branch commit GitHub recorded for the
  pull request; for a long-lived pull request it can be ahead of the commit the branch
  started from, so a task built on it may need the base branch at merge time instead.
- **Backoff has no jitter.** Retries wait fixed exponential times (1, 2, 4, 8 s for server
  and network errors; 60, 120, 240 s for secondary limits, after which the 480 s wait
  exceeds the default `--max-wait` and the run fails), which is deterministic to test but
  can synchronise parallel clients.
- **The ETag cache saves rate limit only with a token.** Without `GITHUB_TOKEN`, a 304
  answer costs one of the 60 hourly requests (three cached runs above: 56, 53, 50 left), so
  a cached re-run of `--limit 29` spends 1 + 2 * 29 = 59 of them like the first run.
- **Fail-to-pass ids are a guess from the diff.** A test whose behaviour changes through a
  helper, a fixture or test data (tomli's `.toml` cases: 14 of its 44 candidates have no
  ids) is not named; a changed line right after a Rust, Go, Java or JavaScript function's
  closing brace is attributed to that function; Python ranges are cut by the first line
  at a lower indentation, so a multi-line string with less indentation ends them early;
  JavaScript `describe` names are not part of the id; Rust `#[test]` functions in pull
  requests (no contents) lose their module path; a Go `t.Run` subtest is not separated
  from its parent. The builder must run the tests to confirm the flip in any case.
- **The report is as long as the export.** Every candidate gets a section and every
  rejected commit a row (`--top` limits the candidates, not the rejections): tomli's
  Markdown report is 102 KB. Feature columns come from the export, so a candidate that
  lacks a feature another one has shows an empty cell.

## Roadmap

Planned in [PLAN.md](PLAN.md), not built yet (slices 1 to 5 are done):

6. Multi-repository batch mining with incremental resume.

## Development

```sh
make check              # lint, typecheck, tests with the coverage gate
make verify-recording   # re-record tomli from GitHub and compare (network)
make record-prs         # re-record the pull-request fixtures from GitHub (network)
UPDATE_GOLDEN=1 uv run pytest tests/test_explain.py tests/test_report.py   # refresh golden files
```

## License

MIT, see [LICENSE](LICENSE). The recorded tomli history and pull-request responses in
`examples/tomli/` come from tomli (MIT, Copyright (c) 2021 Taneli Hukkinen); its license is
in [examples/tomli/LICENSE](examples/tomli/LICENSE).
