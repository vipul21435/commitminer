# Recorded history: hukkin/tomli

`history.jsonl.gz` is the non-merge history of <https://github.com/hukkin/tomli>
(a small TOML parser, MIT license) at commit
`5a77b12a7a9f052ce5a20c335d2825658f6aea52` (2026-04-14, "Use frozendict on Python 3.15"):
312 commits and 5292 changed-file entries. It was recorded with:

```sh
git clone https://github.com/hukkin/tomli
commitminer record tomli --rev 5a77b12a7a9f052ce5a20c335d2825658f6aea52 \
  --repo-name hukkin/tomli --url https://github.com/hukkin/tomli \
  --out examples/tomli/history.jsonl.gz
```

`make verify-recording` repeats those steps from GitHub and checks that the result matches
this file line for line.

What it contains: for each commit the sha, parents, author date, commit message, and per
changed file the path (both paths for renames) with added and deleted line counts, content
signals, and measurements of the file's `--unified=0` patch (hunks, code hunks and lines,
added assertion lines, public declarations touched). It does not store the author and
committer fields, file contents or patch text. Messages are kept verbatim as published
upstream, including any `Co-authored-by` trailers.

tomli is Copyright (c) 2021 Taneli Hukkinen and distributed under the MIT license; the
license text is in [LICENSE](LICENSE) next to this file. The commit messages in the
recording are tomli's.
