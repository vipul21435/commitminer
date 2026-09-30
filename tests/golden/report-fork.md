# CommitMiner report: demo/durations-fork

Source: a local clone of [https://example.invalid/demo/fork.git](https://example.invalid/demo/fork), 5 commits walked. CommitMiner 0.1.0, export schema version 6.

Checked against the ledger ledger.sqlite3 (min overlap 0.5).

## Funnel

| step | count | remaining |
| --- | ---: | ---: |
| walked commits | 5 | 5 |
| rejected: source-unchanged (no source line changed outside inline tests (renames, mode changes, binary files)) | 1 | 4 |
| candidates | 4 | 4 |
| ledger: duplicate | 3 | 1 |
| ledger: new | 1 | 1 |
| exported | 4 | 4 |

Difficulty bands: easy 4.

## Ranked candidates

All 4 exported candidates. Score columns are each feature's contribution (weight times value); f2p counts the likely fail-to-pass tests.

| rank | commit | date | score | difficulty | small\_diff | test\_lines\_added | added\_assertions | linked\_reference | fix\_keyword | focused\_source | lines | src | test | f2p | ledger | subject |
| ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| 1 | [92ee5dce6f](https://example.invalid/demo/fork/commit/92ee5dce6fa8166ca195c383fad07ed0107fcc25) | 2025-03-14 | 7.36 | 0.56 easy | 2.96 | 0.20 | 0.20 | 2.00 | 1.00 | 1.00 | 6 | 1 | 1 | 1 | new | Allow spaces between parts (fixes #3) |
| 2 | [e275649a9e](https://example.invalid/demo/fork/commit/e275649a9ebc6f3c209665b110ab76407b4e678c) | 2025-03-11 | 4.67 | 0.92 easy | 2.92 | 0.35 | 0.40 | 0.00 | 0.00 | 1.00 | 11 | 1 | 1 | 1 | duplicate | Reject numbers without a unit |
| 3 | [6dbfaa8121](https://example.invalid/demo/fork/commit/6dbfaa8121b3c0bb8dcec00e7b83e56ccecb096d) | 2025-03-10 | 4.36 | 1.95 easy | 2.81 | 0.35 | 0.20 | 0.00 | 0.00 | 1.00 | 25 | 1 | 1 | 1 | duplicate | Import durations with two-space indentation |
| 4 | [287f42b780](https://example.invalid/demo/fork/commit/287f42b780d4f713146b8a5066dca8e6bf2b6804) | 2025-03-13 | 4.34 | 0.92 easy | 2.94 | 0.20 | 0.20 | 0.00 | 0.00 | 1.00 | 8 | 1 | 1 | 1 | duplicate | Accept days and weeks |

## Candidates

### #1 92ee5dce6f: Allow spaces between parts (fixes #3)

- commit: [92ee5dce6fa8166ca195c383fad07ed0107fcc25](https://example.invalid/demo/fork/commit/92ee5dce6fa8166ca195c383fad07ed0107fcc25)
- base: 287f42b780d4f713146b8a5066dca8e6bf2b6804
- source files: lib/parse.py
- test files: tests/test\_durations.py
- likely fail-to-pass tests: tests/test\_durations.py::test\_spaces\_between\_parts
- fingerprint: a5e1eeab4abac397 (2 source and test hunks)
- ledger: new

| feature | value | weight | contrib | detail |
| --- | ---: | ---: | ---: | --- |
| small\_diff | 0.985 | 3.00 | 2.955 | 6 of at most 400 source+test lines changed |
| test\_lines\_added | 0.100 | 2.00 | 0.200 | 4 test lines added (full value at 40) |
| added\_assertions | 0.200 | 1.00 | 0.200 | 1 assertion line added in tests (full value at 5) |
| linked\_reference | 1.000 | 2.00 | 2.000 | fixes #3 |
| fix\_keyword | 1.000 | 1.00 | 1.000 | fixes |
| focused\_source | 1.000 | 1.00 | 1.000 | 1 source file changed |

### #2 e275649a9e: Reject numbers without a unit

- commit: [e275649a9ebc6f3c209665b110ab76407b4e678c](https://example.invalid/demo/fork/commit/e275649a9ebc6f3c209665b110ab76407b4e678c)
- base: 6dbfaa8121b3c0bb8dcec00e7b83e56ccecb096d
- source files: src/durations/parse.py
- test files: tests/test\_parse.py
- likely fail-to-pass tests: tests/test\_parse.py::test\_a\_number\_needs\_a\_unit
- fingerprint: d3c6af7bc86c6f62 (3 source and test hunks)
- ledger: duplicate: same fix as demo/durations 536d6c4adf (claimed by alice on 2026-01-02)

| feature | value | weight | contrib | detail |
| --- | ---: | ---: | ---: | --- |
| small\_diff | 0.973 | 3.00 | 2.917 | 11 of at most 400 source+test lines changed |
| test\_lines\_added | 0.175 | 2.00 | 0.350 | 7 test lines added (full value at 40) |
| added\_assertions | 0.400 | 1.00 | 0.400 | 2 assertion lines added in tests (full value at 5) |
| linked\_reference | 0.000 | 2.00 | 0.000 | no issue or pull-request reference |
| fix\_keyword | 0.000 | 1.00 | 0.000 | no fix keyword in the subject |
| focused\_source | 1.000 | 1.00 | 1.000 | 1 source file changed |

### #3 6dbfaa8121: Import durations with two-space indentation

- commit: [6dbfaa8121b3c0bb8dcec00e7b83e56ccecb096d](https://example.invalid/demo/fork/commit/6dbfaa8121b3c0bb8dcec00e7b83e56ccecb096d)
- base: (root commit)
- source files: src/durations/parse.py
- test files: tests/test\_parse.py
- likely fail-to-pass tests: tests/test\_parse.py::test\_hours\_and\_minutes
- fingerprint: 7842e726a4b52616 (2 source and test hunks)
- ledger: duplicate: same fix as demo/durations a4320cdcff (claimed by alice on 2026-01-02)

| feature | value | weight | contrib | detail |
| --- | ---: | ---: | ---: | --- |
| small\_diff | 0.938 | 3.00 | 2.812 | 25 of at most 400 source+test lines changed |
| test\_lines\_added | 0.175 | 2.00 | 0.350 | 7 test lines added (full value at 40) |
| added\_assertions | 0.200 | 1.00 | 0.200 | 1 assertion line added in tests (full value at 5) |
| linked\_reference | 0.000 | 2.00 | 0.000 | no issue or pull-request reference |
| fix\_keyword | 0.000 | 1.00 | 0.000 | no fix keyword in the subject |
| focused\_source | 1.000 | 1.00 | 1.000 | 1 source file changed |

### #4 287f42b780: Accept days and weeks

- commit: [287f42b780d4f713146b8a5066dca8e6bf2b6804](https://example.invalid/demo/fork/commit/287f42b780d4f713146b8a5066dca8e6bf2b6804)
- base: 79d5e2b49c8370fd6eff6201db66826913068e86
- source files: lib/parse.py
- test files: tests/test\_durations.py
- likely fail-to-pass tests: tests/test\_durations.py::test\_days\_and\_weeks
- fingerprint: c282f61bc585bf9b (3 source and test hunks)
- ledger: duplicate: same fix as demo/durations 0287bf15bc (claimed by alice on 2026-01-02)

| feature | value | weight | contrib | detail |
| --- | ---: | ---: | ---: | --- |
| small\_diff | 0.980 | 3.00 | 2.940 | 8 of at most 400 source+test lines changed |
| test\_lines\_added | 0.100 | 2.00 | 0.200 | 4 test lines added (full value at 40) |
| added\_assertions | 0.200 | 1.00 | 0.200 | 1 assertion line added in tests (full value at 5) |
| linked\_reference | 0.000 | 2.00 | 0.000 | no issue or pull-request reference |
| fix\_keyword | 0.000 | 1.00 | 0.000 | no fix keyword in the subject |
| focused\_source | 1.000 | 1.00 | 1.000 | 1 source file changed |

## Rejected commits

### source-unchanged (1): no source line changed outside inline tests (renames, mode changes, binary files)

| commit | date | subject |
| --- | --- | --- |
| [79d5e2b49c](https://example.invalid/demo/fork/commit/79d5e2b49c8370fd6eff6201db66826913068e86) | 2025-03-12 | Move the package to lib/ |

## Settings

| setting | value |
| --- | --- |
| assertions\_cap | 5 |
| cross\_file\_cap | 4 |
| difficulty\_weights | cross\_file 2, files 1, hunks 3, lines 3, public\_api 1 |
| files\_cap | 10 |
| hard\_at | 4.5 |
| hunks\_cap | 10 |
| lines\_cap | 100 |
| max\_lines | 400 |
| max\_source\_files | 10 |
| medium\_at | 2 |
| test\_lines\_cap | 40 |
| weights | added\_assertions 1, fix\_keyword 1, focused\_source 1, linked\_reference 2, small\_diff 3, test\_lines\_added 2 |
