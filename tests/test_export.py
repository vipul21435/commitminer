from __future__ import annotations

import json
from pathlib import Path

from commitminer.export import (
    SCHEMA_VERSION,
    candidate_to_json,
    render_explanation,
    render_summary,
    render_table,
    write_jsonl,
)
from commitminer.models import Commit, FileChange
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
        "rule": "py-test-dir",
        "added": 12,
        "deleted": 0,
    }
    assert "old_path" not in record["files"][0]
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
        "r: walked 3 commits, 2 candidates, 1 rejected (no-source 1)"
    )
    table = render_table(result, top=1).splitlines()
    assert table[0].split() == ["rank", "score", "sha", "date", "lines", "src", "test", "subject"]
    assert len(table) == 2
    assert table[1].split()[:7] == ["1", "7.77", "aaaaaaaaaa", "2024-02-03", "17", "1", "1"]
    assert table[1].endswith("xxx...")
    assert len(table[1].split("  ")[-1]) == 56
    explanation = render_explanation(result.candidates[1], 2).splitlines()
    # 17 of 400 lines: 1 - 17/400 = 0.9575, times 3 = 2.8725; no fix keyword in "Cafe".
    assert explanation[0] == "#2 bbbbbbbbbb score 6.77: Caf?"
    assert explanation[2].split()[:4] == ["small_diff", "0.958", "3.00", "2.873"]
    assert len(explanation) == 7


def test_render_summary_without_rejections() -> None:
    assert render_summary(mine([]), "r") == "r: walked 0 commits, 0 candidates, 0 rejected"
