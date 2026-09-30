"""Ledger schema 2: batch runs recorded with watermarks, and claims of proposed fixes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commitminer import ledger as ledger_module
from commitminer.cli import app
from commitminer.ledger import (
    Ledger,
    LedgerError,
    Proposal,
    Status,
    Watermark,
    open_ledger,
    watermark_to_json,
)
from test_ledger import NOW, clock, fp, proposal

runner = CliRunner()
HEAD = "c" * 40


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "team.sqlite3"


@pytest.fixture
def ledger(path: Path) -> Ledger:
    return open_ledger(path, now=clock)


def test_a_run_records_new_fixes_as_proposed_with_its_watermark(ledger: Ledger) -> None:
    results = ledger.record_run(
        "demo/up",
        "clone",
        HEAD,
        ["a" * 40, "b" * 40, "a" * 40],
        [proposal("a", "h1", "h2"), proposal("b", "x1")],
        owner="batch",
    )
    assert [(entry is not None, verdict.status) for entry, verdict in results] == [
        (True, Status.NEW),
        (True, Status.NEW),
    ]
    assert [(e.status, e.owner) for e in ledger.entries()] == [("proposed", "batch")] * 2
    assert ledger.watermark("demo/up", "clone") == Watermark(
        "demo/up", "clone", HEAD, walked=2, runs=1, updated=NOW
    )
    assert ledger.walked_shas("demo/up", "clone") == {"a" * 40, "b" * 40}
    # Other sources of the same repository keep their own watermark.
    assert ledger.watermark("demo/up", "history") is None
    assert ledger.walked_shas("demo/up", "history") == frozenset()


def test_a_second_run_moves_the_watermark_and_adds_up_the_walked_commits(ledger: Ledger) -> None:
    ledger.record_run("demo/up", "clone", "1" * 40, ["a" * 40], [proposal("a", "h1")])
    ledger.record_run("demo/up", "clone", "2" * 40, ["b" * 40, "c" * 40], [])
    mark = ledger.watermark("demo/up", "clone")
    assert mark is not None
    assert (mark.position, mark.walked, mark.runs) == ("2" * 40, 3, 2)
    ledger.record_run("demo/fork", "history", "9" * 40, [], [])
    assert [(m.repo, m.source) for m in ledger.watermarks()] == [
        ("demo/fork", "history"),
        ("demo/up", "clone"),
    ]
    assert watermark_to_json(ledger.watermarks()[0]) == {
        "repo": "demo/fork",
        "source": "history",
        "position": "9" * 40,
        "walked": 0,
        "runs": 1,
        "updated": NOW,
    }


def test_proposals_are_compared_with_the_ledger_and_earlier_proposals(ledger: Ledger) -> None:
    ledger.add(proposal("a", "h1", "h2"), owner="alice")
    results = ledger.record_run(
        "demo/fork",
        "history",
        HEAD,
        [],
        [
            proposal("b", "h2", "h1", repo="demo/fork"),  # upstream's fix, other commit
            proposal("c", "y1", "y2", repo="demo/fork"),  # new
            proposal("d", "y1", "y2", "y3", repo="demo/fork"),  # overlaps c, from this run
            proposal("a", "h1", "h2", repo="demo/fork"),  # upstream's commit, in a fork
            Proposal("demo/fork", "e" * 40, "fix e", None),  # no fingerprint
        ],
        min_overlap=0.5,
    )
    statuses = [verdict.status for _, verdict in results]
    assert statuses == [
        Status.DUPLICATE,
        Status.NEW,
        Status.OVERLAP,
        Status.DUPLICATE,
        Status.UNKNOWN,
    ]
    assert [entry is not None for entry, _ in results] == [False, True, False, False, False]
    assert results[0][1].describe("b" * 40, "demo/fork") == (
        "duplicate: same fix as demo/up aaaaaaaaaa (claimed by alice on 2026-01-02)"
    )
    assert results[2][1].describe("d" * 40, "demo/fork") == (
        "overlap: 2 of 2 hunks shared with demo/fork cccccccccc (earlier in this run)"
    )
    assert results[3][1].describe("a" * 40, "demo/fork") == (
        "duplicate: same commit as demo/up aaaaaaaaaa (claimed by alice on 2026-01-02)"
    )
    # Without the candidate's repository, a match on the same commit reads as before.
    assert results[3][1].describe("a" * 40).startswith("duplicate: already in the ledger")


def test_a_commit_walked_again_is_already_recorded(ledger: Ledger, path: Path) -> None:
    ledger.record_run("demo/up", "clone", HEAD, [], [proposal("a", "h1")])
    ledger.close()
    # A later batch with --full walks it again.
    ledger = open_ledger(path, now=clock)
    (again,) = ledger.record_run("demo/up", "clone", HEAD, [], [proposal("a", "h1")])
    assert again[0] is None
    assert (
        again[1]
        .describe("a" * 40, "demo/up")
        .startswith("duplicate: already in the ledger (proposed on 2026-01-02)")
    )
    # Reclassified (another fingerprint): still the same commit, not a new fix.
    ((entry, verdict),) = ledger.record_run("demo/up", "clone", HEAD, [], [proposal("a", "z1")])
    assert entry is None
    assert verdict.status is Status.DUPLICATE
    assert not verdict.matches[0].exact
    # The same commit recorded earlier in this run, from another source of the repository.
    first, second = ledger.record_run(
        "demo/up",
        "pull-requests",
        "2026-01-01T00:00:00Z",
        [],
        [proposal("b", "k1"), proposal("b", "k1")],
    )
    assert first[1].status is Status.NEW
    assert second[1].matches[0].in_run
    assert second[1].describe("b" * 40, "demo/up") == (
        "duplicate: same commit as demo/up bbbbbbbbbb (earlier in this run)"
    )


def test_a_failed_run_keeps_nothing(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger.record_run("demo/up", "clone", "1" * 40, ["a" * 40], [proposal("a", "h1")])
    calls = 0
    original = ledger._insert

    def fails_second(*args: object) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("disk on fire")
        return original(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(ledger, "_insert", fails_second)
    with pytest.raises(RuntimeError, match="disk on fire"):
        ledger.record_run(
            "demo/up", "clone", "2" * 40, ["b" * 40], [proposal("b", "x1"), proposal("c", "y1")]
        )
    monkeypatch.undo()
    assert [e.sha[0] for e in ledger.entries()] == ["a"]
    mark = ledger.watermark("demo/up", "clone")
    assert mark is not None
    assert (mark.position, mark.walked, mark.runs) == ("1" * 40, 1, 1)
    assert ledger.walked_shas("demo/up", "clone") == {"a" * 40}
    # The rolled-back entry is not remembered as part of the run.
    ((_, verdict),) = ledger.record_run("demo/up", "clone", "2" * 40, [], [proposal("b", "x1")])
    assert verdict.status is Status.NEW


def test_a_snapshot_records_runs_without_touching_the_file(ledger: Ledger, path: Path) -> None:
    ledger.record_run("demo/up", "clone", "1" * 40, ["a" * 40], [proposal("a", "h1")])
    scratch = ledger.snapshot()
    try:
        scratch.record_run("demo/up", "clone", "2" * 40, ["b" * 40], [proposal("b", "x1")])
        mark = scratch.watermark("demo/up", "clone")
        assert mark is not None
        assert mark.position == "2" * 40
    finally:
        scratch.close()
    with open_ledger(path) as reopened:
        assert len(reopened.entries()) == 1
        mark = reopened.watermark("demo/up", "clone")
        assert mark is not None
        assert mark.position == "1" * 40


def test_claiming_a_proposed_fix_takes_it_over(ledger: Ledger, path: Path) -> None:
    ledger.record_run("demo/up", "history", HEAD, [], [proposal("a", "h1", "h2")])
    ledger.record_run("demo/up", "clone", HEAD, [], [proposal("c", "k1", "k2")])
    ledger.close()
    # Later, an author claims: the fork's copy of the same fix takes the proposed entry.
    ledger = open_ledger(path, now=clock)
    entry, verdict = ledger.add(proposal("b", "h2", "h1", repo="demo/fork"), owner="bob")
    assert verdict.status is Status.DUPLICATE
    assert entry is not None
    assert (entry.repo, entry.sha[0], entry.status, entry.owner) == (
        "demo/up",
        "a",
        "claimed",
        "bob",
    )
    assert entry.first_seen == NOW
    # Taken now: another claim is refused.
    refused, why = ledger.add(proposal("a", "h1", "h2"), owner="carol")
    assert refused is None
    assert why.describe("a" * 40, "demo/up") == (
        "duplicate: already in the ledger (claimed by bob on 2026-01-02)"
    )
    # Overlaps with a proposed fix are refused as before.
    overlap, seen = ledger.add(proposal("d", "k1", "k9"))
    assert overlap is None
    assert seen.status is Status.OVERLAP


def test_watermark_reads_report_sqlite_errors(ledger: Ledger) -> None:
    ledger.close()
    with pytest.raises(LedgerError, match="cannot read"):
        ledger.watermark("r", "clone")
    with pytest.raises(LedgerError, match="cannot read"):
        ledger.watermarks()
    with pytest.raises(LedgerError, match="cannot read"):
        ledger.walked_shas("r", "clone")


def test_ledger_watermarks_command(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_module, "utc_now", clock)
    empty = runner.invoke(app, ["ledger", "watermarks", str(path)])
    assert empty.exit_code == 2
    assert "no such ledger" in empty.output
    with open_ledger(path) as opened:
        opened.record_run("hukkin/tomli", "history", "5a77b12a7a" + "0" * 30, ["a" * 40], [])
        opened.record_run("hukkin/tomli", "pull-requests", "2026-04-14T10:00:00Z", [], [])
        opened.record_run("x/new", "clone", "", [], [])
    text = runner.invoke(app, ["ledger", "watermarks", str(path)])
    assert text.exit_code == 0, text.output
    assert text.stdout.splitlines() == [
        f"{path}: 3 watermarks",
        "repo                      source         position              walked  runs  updated",
        f"hukkin/tomli              history        5a77b12a7a                 1     1  {NOW}",
        f"hukkin/tomli              pull-requests  2026-04-14T10:00:00Z       0     1  {NOW}",
        f"x/new                     clone          -                          0     1  {NOW}",
    ]
    as_json = runner.invoke(app, ["ledger", "watermarks", str(path), "--json"])
    records = [json.loads(line) for line in as_json.stdout.splitlines()]
    assert [r["source"] for r in records] == ["history", "pull-requests", "clone"]
    fresh = path.with_name("fresh.sqlite3")
    open_ledger(fresh).close()
    none = runner.invoke(app, ["ledger", "watermarks", str(fresh)])
    assert none.stdout == f"{fresh}: 0 watermarks\n"


def test_fp_helper_is_order_insensitive() -> None:
    assert fp("h1", "h2").patch == fp("h2", "h1").patch
