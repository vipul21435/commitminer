"""The committed JSON Schema describes every export CommitMiner writes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from typer.testing import CliRunner

from commitminer import ledger as ledger_module
from commitminer.cli import app
from commitminer.export import SCHEMA_FILE, SCHEMA_VERSION, schema_text
from exportfile import read_export
from test_cli_ledger import BUILDER

ROOT = Path(__file__).resolve().parent.parent
HISTORY = ROOT / "examples" / "tomli" / "history.jsonl.gz"
PRS = ROOT / "examples" / "tomli" / "prs"
runner = CliRunner()


@pytest.fixture(scope="module")
def validator() -> Draft202012Validator:
    schema = json.loads(schema_text())
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def _errors(validator: Draft202012Validator, record: dict[str, Any]) -> list[str]:
    return [
        f"{'/'.join(str(p) for p in error.path)}: {error.message}"
        for error in sorted(validator.iter_errors(record), key=lambda e: list(e.path))
    ]


def _valid(validator: Draft202012Validator, path: Path) -> tuple[dict[str, Any], list[Any]]:
    run, candidates = read_export(path)
    for record in (run, *candidates):
        assert _errors(validator, record) == [], record.get("sha")
    return run, candidates


def test_the_schema_file_is_committed_and_printed_by_the_cli() -> None:
    committed = ROOT / "src" / "commitminer" / "schemas" / SCHEMA_FILE
    assert committed.name == f"export-v{SCHEMA_VERSION}.schema.json"
    result = runner.invoke(app, ["schema"])
    assert result.exit_code == 0, result.output
    assert result.stdout == committed.read_text(encoding="utf-8") == schema_text()
    schema = json.loads(result.stdout)
    assert schema["$defs"]["candidate"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    assert schema["$defs"]["run"]["properties"]["schema_version"]["const"] == SCHEMA_VERSION
    # Every field the export writes is described: nothing else is allowed.
    assert schema["$defs"]["candidate"]["additionalProperties"] is False
    assert schema["$defs"]["run"]["additionalProperties"] is False


def test_the_recorded_history_export_validates(
    validator: Draft202012Validator, tmp_path: Path
) -> None:
    out = tmp_path / "tomli.jsonl"
    result = runner.invoke(
        app, ["mine", "--history", str(HISTORY), "--top", "0", "--explain", "0", "--out", str(out)]
    )
    assert result.exit_code == 0, result.output
    run, candidates = _valid(validator, out)
    assert (run["walked"], run["candidates"], run["exported"]) == (312, 44, 44)
    assert run["url"] == "https://github.com/hukkin/tomli"
    assert sum(run["rejected"].values()) == len(run["rejections"]) == 268
    assert run["ledger"] is None
    assert run["settings"]["weights"]["small_diff"] == 3.0
    with_tests = [c for c in candidates if c["fail_to_pass"]]
    assert len(with_tests) == 30
    assert candidates[0]["fail_to_pass"] == [
        "tests/test_misc.py::test_deepcopy",
        "tests/test_misc.py::test_parse_float",
    ]


def test_the_pull_request_export_validates(validator: Draft202012Validator, tmp_path: Path) -> None:
    out = tmp_path / "prs.jsonl"
    args = ["prs", "hukkin/tomli", "--limit", "25", "--replay", str(PRS), "--top", "0"]
    result = runner.invoke(app, [*args, "--explain", "0", "--out", str(out)])
    assert result.exit_code == 0, result.output
    run, candidates = _valid(validator, out)
    assert run["source"] == "pull-requests"
    assert all(c["pull_request"] is not None for c in candidates)
    assert all(c["repo_url"] == "https://github.com/hukkin/tomli" for c in candidates)


def test_the_ledger_exports_validate(
    validator: Draft202012Validator, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_module, "utc_now", lambda: "2026-01-02T03:04:05+00:00")
    root = tmp_path / "repos"
    BUILDER.build(root)
    upstream, fork, ledger = tmp_path / "up.jsonl", tmp_path / "fork.jsonl", tmp_path / "l.sqlite3"
    for args in (
        ["mine", str(root / "upstream"), "--repo-name", "demo/durations", "--out", str(upstream)],
        ["ledger", "add", str(ledger), str(upstream), "--owner", "alice"],
        ["mine", str(root / "fork"), "--ledger", str(ledger), "--out", str(fork), "--new-only"],
    ):
        result = runner.invoke(app, [*args, "--explain", "0"] if args[0] == "mine" else args)
        assert result.exit_code == 0, result.output
    _valid(validator, upstream)
    run, candidates = _valid(validator, fork)
    assert run["ledger"] == {
        "path": str(ledger),
        "min_overlap": 0.5,
        "new_only": True,
        "counts": {"new": 1, "duplicate": 3, "overlap": 0, "unknown": 0},
    }
    assert (run["candidates"], run["exported"], len(candidates)) == (4, 1, 1)
    # A duplicate's ledger match, as exported without --new-only.
    full = tmp_path / "fork-all.jsonl"
    result = runner.invoke(
        app, ["mine", str(root / "fork"), "--ledger", str(ledger), "--out", str(full), "--top", "0"]
    )
    assert result.exit_code == 0, result.output
    _, all_candidates = _valid(validator, full)
    assert {c["ledger"]["status"] for c in all_candidates} == {"new", "duplicate"}


def test_the_schema_rejects_what_the_export_never_writes(validator: Draft202012Validator) -> None:
    run = {
        "kind": "run",
        "schema_version": SCHEMA_VERSION,
        "commitminer": "0.1.0",
        "repo": "r",
        "url": None,
        "source": "clone",
        "unit": "commits",
        "walked": 1,
        "candidates": 0,
        "bands": {},
        "rejected": {"docs-only": 1},
        "rejections": [
            {
                "sha": "a" * 40,
                "date": "2024-01-01T00:00:00+00:00",
                "subject": "s",
                "reason": "docs-only",
                "pull_request": None,
            }
        ],
        "ledger": None,
        "exported": 0,
        "settings": {
            "max_lines": 400,
            "max_source_files": 10,
            "test_lines_cap": 40,
            "assertions_cap": 5,
            "files_cap": 10,
            "hunks_cap": 10,
            "lines_cap": 100,
            "cross_file_cap": 4,
            "medium_at": 2.0,
            "hard_at": 4.5,
            "weights": {"small_diff": 3.0},
            "difficulty_weights": {"files": 1.0},
        },
    }
    assert _errors(validator, run) == []
    assert _errors(validator, {**run, "rejected": {"bogus": 1}})
    assert _errors(validator, {**run, "source": "web"})
    assert _errors(validator, {**run, "extra": 1})
    assert _errors(validator, {**run, "kind": "candidate"})
    assert _errors(validator, {**run, "rejections": [{**run["rejections"][0], "sha": "short"}]})
