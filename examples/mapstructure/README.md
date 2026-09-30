# Recorded histories: mitchellh/mapstructure and its fork go-viper/mapstructure

A small Go library (decoding generic maps into structs, MIT license) and the community
fork that continued it after the original was archived. They are the second demo language
of `make demo` and its cross-repository dedupe case: the fork's history contains every
commit of the original, plus its own.

| File | Repository | Head | Commits | File entries |
| --- | --- | --- | --- | --- |
| `mitchellh.jsonl.gz` | <https://github.com/mitchellh/mapstructure> (archived) | `8508981c8b6c964e6986dd8aa85490e70ce3c2e2` (2023-12-16) | 236 | 364 |
| `go-viper.jsonl.gz` | <https://github.com/go-viper/mapstructure> | `52aa5c6dc1d27226460807054ca2107b2d54fb2d` (2026-01-27) | 374 | 589 |

Both are non-merge histories recorded with:

```sh
git clone https://github.com/mitchellh/mapstructure mitchellh
commitminer record mitchellh --rev 8508981c8b6c964e6986dd8aa85490e70ce3c2e2 \
  --repo-name mitchellh/mapstructure --url https://github.com/mitchellh/mapstructure \
  --out examples/mapstructure/mitchellh.jsonl.gz
git clone https://github.com/go-viper/mapstructure go-viper
commitminer record go-viper --rev 52aa5c6dc1d27226460807054ca2107b2d54fb2d \
  --repo-name go-viper/mapstructure --url https://github.com/go-viper/mapstructure \
  --out examples/mapstructure/go-viper.jsonl.gz
```

`make verify-recording` repeats those steps from GitHub and checks that both results match
these files byte for byte. What a recording holds is described in
[../tomli/README.md](../tomli/README.md): per commit the sha, parents, author date and
message, and per changed file its path, line counts, content signals and patch
measurements, with one 64-bit hash per hunk. No file contents, patch text, author or
committer fields.

mapstructure is Copyright (c) 2013 Mitchell Hashimoto and distributed under the MIT
license; the license text (the same in both repositories) is in [LICENSE](LICENSE) next to
this file. The commit messages in the recordings are the two projects'.
