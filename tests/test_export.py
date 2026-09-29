from __future__ import annotations

import json
from pathlib import Path

import pytest

from commitminer.export import (
    SCHEMA_VERSION,
    candidate_to_json,
    render_explanation,
    render_ledger,
    render_summary,
    render_table,
    write_jsonl,
)
from commitminer.ledger import Entry, Match, Status, Verdict
from commitminer.models import Commit, FileChange, PatchStats
from commitminer.scoring import mine

FILES = (
    FileChange("src/pkg/parser.py", 4, 1),
    FileChange("tests/test_new.py", 12, 0, old_path="tests/test_old.py"),
    FileChange("CHANGELOG.md", 2, 0),
)


def _commit(sha: str, subject: str, date: str = "2024-02-03T04:05:06+00:00") -> Commit:
    return Commit(sha, ("f" * 40,), date, f"{subject}\n\nfixes #9\n", FILES)


def test_candidate_json_has_everything_a_builder_needs() -> None:
    result = mine([_commit("a" * 40, "Fix parser")])
    record = candidate_to_json(result.candidates[0], 1, "demo/repo")
    assert record["schema_version"] == SCHEMA_VERSION
    assert record["rank"] == 1
    assert record["repo"] == "demo/repo"
    assert record["sha"] == "a" * 40
    assert record["base"] == "f" * 40
    assert record["subject"] == "Fix parser"
    assert record["source_files"] == ["src/pkg/parser.py"]
    assert record["test_files"] == ["tests/test_new.py"]
    assert record["lines"] == {
        "changed": 17,
        "source_added": 4,
        "source_deleted": 1,
        "test_added": 12,
        "test_deleted": 0,
    }
    assert record["files"][1] == {
        "path": "tests/test_new.py",
        "old_path": "tests/test_old.py",
        "category": "test",
        "rule": "test-dir",
        "added": 12,
        "deleted": 0,
    }
    assert "old_path" not in record["files"][0]
    assert "signals" not in record["files"][0]
    assert record["inline_test_files"] == []
    total = sum(f["contribution"] for f in record["features"])
    assert round(total, 4) == record["score"]


def test_write_jsonl_ranks_and_is_ascii(tmp_path: Path) -> None:
    result = mine([_commit("a" * 40, "Fix one"), _commit("b" * 40, "Fix caf\u00e9 parsing (#3)")])
    out = tmp_path / "nested" / "c.jsonl"
    assert write_jsonl(out, result, "r") == 2
    text = out.read_text(encoding="ascii")
    records = [json.loads(line) for line in text.splitlines()]
    assert [r["rank"] for r in records] == [1, 2]
    assert records[0]["sha"] == "a" * 40  # same score, same date: sha breaks the tie
    assert records[1]["subject"] == "Fix caf\u00e9 parsing (#3)"


def test_write_jsonl_with_no_candidates_writes_an_empty_file(tmp_path: Path) -> None:
    out = tmp_path / "empty.jsonl"
    assert write_jsonl(out, mine([]), "r") == 0
    assert out.read_bytes() == b""


def test_render_summary_table_and_explanation() -> None:
    long_subject = "Fix " + "x" * 80
    rejected = Commit("c" * 40, (), "2024-01-01T00:00:00+00:00", "docs", (FILES[2],))
    result = mine([_commit("a" * 40, long_subject), _commit("b" * 40, "Caf\u00e9"), rejected])
    assert render_summary(result, "r") == (
        "r: walked 3 commits, 2 candidates (easy 2), 1 rejected (docs-only 1)"
    )
    table = render_table(result, top=1).splitlines()
    assert table[0].split() == [
        "rank",
        "score",
        "diff",
        "sha",
        "date",
        "lines",
        "src",
        "test",
        "subject",
    ]
    assert len(table) == 2
    assert table[1].split()[:9] == [
        "1",
        "7.47",
        "0.20",
        "easy",
        "aaaaaaaaaa",
        "2024-02-03",
        "17",
        "1",
        "1",
    ]
    assert table[1].endswith("xxx...")
    assert len(table[1].split("  ")[-1]) == 44
    explanation = render_explanation(result.candidates[1], 2).splitlines()
    # 17 of 400 lines: 1 - 17/400 = 0.9575, times 3 = 2.8725; no fix keyword in "Cafe".
    assert explanation[0] == "#2 bbbbbbbbbb score 6.47, difficulty 0.20 (easy): Caf?"
    assert explanation[1:3] == ["  score", "    feature            value weight contrib  detail"]
    assert explanation[3].split()[:4] == ["small_diff", "0.958", "3.00", "2.873"]
    assert explanation[9].split() == ["total", "10.00", "6.473"]
    assert explanation[10] == "  difficulty"
    assert explanation[-1].split() == ["total", "10.00", "0.200"]
    assert len(explanation) == 18
    assert render_explanation(result.candidates[1]).startswith("bbbbbbbbbb score 6.47")


def test_render_summary_without_rejections() -> None:
    assert render_summary(mine([]), "r") == "r: walked 0 commits, 0 candidates, 0 rejected"


def test_signals_and_inline_test_files_are_exported() -> None:
    lib = FileChange("src/lib.rs", 8, 1, signals=("rust-inline-tests", "rust-tests-added"))
    commit = Commit("c" * 40, ("f" * 40,), "2024-02-03T04:05:06+00:00", "Fix add\n", (lib,))
    record = candidate_to_json(mine([commit]).candidates[0], 1, "demo/rust")
    assert record["inline_test_files"] == ["src/lib.rs"]
    assert record["test_files"] == []
    assert record["files"][0]["signals"] == ["rust-inline-tests", "rust-tests-added"]
    assert record["files"][0]["rule"] == "rust-inline-tests"
    row = render_table(mine([commit]), top=1).splitlines()[1]
    assert row.split()[6:9] == ["9", "1", "0+1"]


def test_patch_data_and_difficulty_are_exported() -> None:
    source = FileChange("src/pkg/api.py", 6, 2, patch=PatchStats(2, 1, 5, 2, api=("def load",)))
    test = FileChange("tests/test_api.py", 9, 0, patch=PatchStats(1, 1, 8, 0, asserts=3))
    commit = Commit(
        "d" * 40, ("f" * 40,), "2024-02-03T04:05:06+00:00", "Fix load\n", (source, test)
    )
    record = candidate_to_json(mine([commit]).candidates[0], 1, "r")
    assert record["files"][0]["patch"] == {
        "hunks": 2,
        "code_hunks": 1,
        "code_added": 5,
        "code_deleted": 2,
        "test_added": 0,
        "test_deleted": 0,
        "asserts": 0,
        "api": ["def load"],
    }
    assert record["public_api"] == ["def load"]
    difficulty = record["difficulty"]
    assert difficulty["band"] == "easy"
    assert [f["name"] for f in difficulty["features"]] == [
        "files",
        "hunks",
        "lines",
        "cross_file",
        "public_api",
    ]
    total = sum(f["contribution"] for f in difficulty["features"])
    assert round(total, 4) == difficulty["value"] == 1.71
    assert [f["name"] for f in record["features"]][2] == "added_assertions"


def test_ledger_verdicts_in_the_table_the_report_and_the_export(tmp_path: Path) -> None:
    result = mine([_commit("a" * 40, "Fix parser"), _commit("b" * 40, "Fix lexer")])
    entry = Entry(1, "f", "demo/up", "c" * 40, "Fix parser", "claimed", "alice", "2026-01-02", 2)
    verdicts = [Verdict(Status.NEW), Verdict(Status.DUPLICATE, (Match(entry, 2, 2, True),))]
    header, *rows = render_table(result, top=5, verdicts=verdicts).splitlines()
    assert header.endswith("  test  ledger   subject")
    assert [row.split()[9] for row in rows] == ["new", "dup"]
    assert render_ledger(result, verdicts, "team.sqlite3").splitlines() == [
        "ledger team.sqlite3: 1 new, 1 duplicate",
        f"  #2 {result.candidates[1].commit.sha[:10]} duplicate: same fix as demo/up "
        "cccccccccc (claimed by alice on 2026-01-02)",
    ]
    assert render_ledger(mine([]), [], "x") == "ledger x: no candidates"
    out = tmp_path / "c.jsonl"
    write_jsonl(out, result, "r", verdicts, ranks=[3, 7])
    first, second = (json.loads(line) for line in out.read_text().splitlines())
    assert (first["rank"], first["ledger"]) == (3, {"matches": [], "status": "new"})
    assert second["ledger"]["matches"][0]["sha"] == "c" * 40
    with pytest.raises(ValueError, match="one ledger verdict per candidate"):
        render_table(result, top=5, verdicts=verdicts[:1])
