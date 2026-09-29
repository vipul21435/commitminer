# CommitMiner

[![CI](https://github.com/vipul21435/commitminer/actions/workflows/ci.yml/badge.svg)](https://github.com/vipul21435/commitminer/actions/workflows/ci.yml)

Mine and rank candidate fail-to-pass tasks from real repository history, so task authors
spend time only on commits that can become good tasks.

A fail-to-pass task is built from a real fix: the tests added or changed by a commit fail on
the parent commit and pass on the fix. Finding those commits by hand is slow. CommitMiner
will walk a repository's history, classify every changed file, keep commits that change
source and tests together, score them with a transparent model that shows each feature's
contribution, drop fixes that were already proposed (across repositories, forks and
cherry-picks) using a shared ledger, and export the ranked candidates as JSONL for
downstream environment builders.

## Status

This is the project scaffold. What exists and works today:

- A Python 3.12 package (`src/` layout, uv, `uv.lock` committed) with a Typer CLI that has
  one command, `commitminer version`.
- Tooling: ruff (lint and format), `mypy --strict` on `src/`, pytest with a 90% coverage
  gate, pre-commit hooks, a Makefile, and GitHub Actions CI (lint, typecheck, tests with
  coverage, `make demo`, and a Docker build job).
- A Docker image on digest-pinned `python:3.12-slim` and `uv` bases that runs the CLI as a
  non-root user (uid 10001).

Everything else, including the history walker, the classifier, the scorer, the ledger and
the reports, is planned in [PLAN.md](PLAN.md) and not built yet. `make demo` is currently a
CLI smoke run; the core deliverable replaces it with a demo on the recorded history of a
small public MIT-licensed repository.

## Quickstart

```sh
make install     # uv sync --locked + pre-commit install
make check       # lint, typecheck, tests with the coverage gate
make demo        # CLI smoke run for now
make docker      # build the image, run it, prune this project's dangling images
```

Measured on the scaffold commit:

| What | Command | Result |
| --- | --- | --- |
| Tests | `make cov` | 4 passed, 100% coverage (gate 90%) |
| Image size | `docker image inspect commitminer:local --format '{{.Size}}'` | 110359052 bytes |

## Roadmap

See [PLAN.md](PLAN.md): the core deliverable (local history walker, Python file classifier,
candidate filter, transparent scorer, JSONL export, recorded-history demo), then six
slices: multi-language classifier, patch-level difficulty features, patch fingerprints with
a SQLite dedupe ledger, a GitHub merged pull-request walker, the export schema with
Markdown/HTML reports, and multi-repository batch mining.

## License

MIT, see [LICENSE](LICENSE).
