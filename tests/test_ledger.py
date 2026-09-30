"""The SQLite ledger: schema, claims, verdicts, snapshots and concurrent writers."""

from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from commitminer import ledger as ledger_module
from commitminer.fingerprint import FINGERPRINT_VERSION, Fingerprint
from commitminer.ledger import (
    APPLICATION_ID,
    SCHEMA_VERSION,
    Ledger,
    LedgerError,
    Match,
    Proposal,
    Status,
    Verdict,
    check_all,
    entry_to_json,
    ledger_verdict_to_json,
    open_ledger,
    read_candidates,
    utc_now,
)

NOW = "2026-01-02T03:04:05+00:00"


def clock() -> str:
    return NOW


def fp(*hunks: str) -> Fingerprint:
    return Fingerprint.of(hunks)


def proposal(sha: str, *hunks: str, repo: str = "demo/up") -> Proposal:
    return Proposal(repo, sha * 40 if len(sha) == 1 else sha, f"fix {sha}", fp(*hunks))


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "team.sqlite3"


@pytest.fixture
def ledger(path: Path) -> Ledger:
    return open_ledger(path, now=clock)


def test_a_new_ledger_gets_the_versioned_schema(path: Path) -> None:
    with open_ledger(path) as opened:
        assert opened.schema_version == SCHEMA_VERSION == 2
        assert opened.entries() == []
        assert opened.watermarks() == []
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"meta", "entries", "hunks", "watermarks", "walked"}
    assert db.execute("SELECT value FROM meta").fetchall() == [(str(FINGERPRINT_VERSION),)]
    db.close()
    # Opening again keeps what is there.
    with open_ledger(path) as again:
        assert again.schema_version == SCHEMA_VERSION


def test_add_records_an_entry_and_refuses_the_same_fix(ledger: Ledger) -> None:
    entry, verdict = ledger.add(proposal("a", "h1", "h2", "h2"), owner="alice")
    assert verdict.status is Status.NEW
    assert entry is not None
    assert (entry.repo, entry.status, entry.owner, entry.first_seen) == (
        "demo/up",
        "claimed",
        "alice",
        NOW,
    )
    assert entry.hunk_count == 2  # distinct hunks
    assert entry.fingerprint == fp("h1", "h2", "h2").patch
    # A cherry-pick of the same fix in another repository: same fingerprint.
    again, refused = ledger.add(proposal("b", "h2", "h1", "h2", repo="demo/fork"), owner="bob")
    assert again is None
    assert refused.status is Status.DUPLICATE
    assert refused.describe("b" * 40) == (
        "duplicate: same fix as demo/up aaaaaaaaaa (claimed by alice on 2026-01-02)"
    )
    assert refused.describe("a" * 40) == (
        "duplicate: already in the ledger (claimed by alice on 2026-01-02)"
    )
    assert [e.sha for e in ledger.entries()] == ["a" * 40]


def test_partial_overlaps_are_refused_unless_allowed(ledger: Ledger) -> None:
    ledger.add(proposal("a", "h1", "h2", "h3"), status="proposed")
    entry, verdict = ledger.add(proposal("b", "h1", "h2", "x9"))
    assert entry is None
    assert verdict.status is Status.OVERLAP
    (match,) = verdict.matches
    assert (match.shared, match.smaller, round(match.overlap, 2)) == (2, 3, 0.67)
    assert verdict.describe("b" * 40) == (
        "overlap: 2 of 3 hunks shared with demo/up aaaaaaaaaa (proposed on 2026-01-02)"
    )
    # Below the threshold is new; with --allow-overlap only exact duplicates are refused.
    assert ledger.add(proposal("c", "h1", "y1", "y2"))[1].status is Status.NEW
    added, noted = ledger.add(proposal("d", "h1", "h2", "x9"), min_overlap=None)
    assert added is not None
    assert noted.status is Status.NEW
    exact = ledger.add(proposal("e", "h1", "h2", "h3"), status="proposed", min_overlap=None)
    assert exact[0] is None
    assert exact[1].status is Status.DUPLICATE


def test_overlap_is_measured_against_the_smaller_fix(ledger: Ledger) -> None:
    ledger.add(proposal("a", "h1", "h2"))
    # A squash of the fix with more work: all of the smaller one is inside it.
    verdict = ledger.verdict(fp("h1", "h2", "x1", "x2", "x3"), 0.5)
    assert verdict.status is Status.OVERLAP
    assert (verdict.matches[0].shared, verdict.matches[0].smaller) == (2, 2)
    assert ledger.verdict(fp("h1", "x1", "x2"), 0.75).status is Status.NEW


def test_matches_are_ordered_exact_first_then_by_overlap(ledger: Ledger) -> None:
    ledger.add(proposal("a", "h1", "h2", "h3", "h4"))
    ledger.add(proposal("b", "h1", "h2", "x1"), min_overlap=None)
    ledger.add(proposal("c", "h1", "h2", "h3", "h4", "h5"), min_overlap=None)
    verdict = ledger.verdict(fp("h1", "h2", "h3", "h4"), 0.5)
    assert verdict.status is Status.DUPLICATE
    assert [m.entry.sha[0] for m in verdict.matches] == ["a", "c", "b"]
    assert [m.exact for m in verdict.matches] == [True, False, False]
    assert verdict.describe("z" * 40).endswith("(+2 more)")
    # Exact only: no partial matches are looked up.
    assert len(ledger.verdict(fp("h1", "h2", "h3", "h4"), None).matches) == 1


def test_the_same_commit_with_another_fingerprint_is_refused(ledger: Ledger) -> None:
    ledger.add(proposal("a", "h1"))
    # Another classification of the same commit: the (repo, sha) constraint refuses it.
    entry, verdict = ledger.add(proposal("a", "z1", "z2"))
    assert entry is None
    assert verdict.status is Status.DUPLICATE
    assert not verdict.matches[0].exact
    assert verdict.describe("a" * 40).startswith("duplicate: already in the ledger")


def test_the_fingerprint_is_unique_at_the_schema_level(ledger: Ledger, path: Path) -> None:
    ledger.add(proposal("a", "h1"))
    db = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError, match=r"UNIQUE constraint failed: entries\."):
        db.execute(
            "INSERT INTO entries (fingerprint, repo, sha, subject, status, first_seen, hunk_count)"
            " VALUES (?, 'x', 'y', 's', 'claimed', 't', 1)",
            (fp("h1").patch,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
        db.execute(
            "INSERT INTO entries (fingerprint, repo, sha, subject, status, first_seen, hunk_count)"
            " VALUES ('f', 'x', 'y', 's', 'done', 't', 1)"
        )
    db.close()


def test_concurrent_claims_of_one_fix_have_exactly_one_winner(path: Path) -> None:
    open_ledger(path).close()
    barrier = threading.Barrier(8)
    results: list[bool] = []
    lock = threading.Lock()

    def claim(index: int) -> None:
        with open_ledger(path, timeout=30) as mine:
            barrier.wait()
            entry, _ = mine.add(proposal(f"{index:040d}", "h1", "h2", repo=f"r{index}"))
        with lock:
            results.append(entry is not None)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results) == [False] * 7 + [True]
    with open_ledger(path) as opened:
        assert len(opened.entries()) == 1


def test_unknown_fingerprints_and_statuses(ledger: Ledger) -> None:
    blank = Proposal("r", "s" * 40, "fix", None)
    entry, verdict = ledger.add(blank)
    assert entry is None
    assert verdict == Verdict(Status.UNKNOWN)
    assert verdict.describe("s").startswith("unknown: no fingerprint")
    assert Verdict(Status.NEW).describe("s") == "new"
    with pytest.raises(LedgerError, match="unknown status 'done'"):
        ledger.add(proposal("a", "h1"), status="done")


def test_entries_by_repository(ledger: Ledger) -> None:
    ledger.add(proposal("a", "h1"))
    ledger.add(proposal("b", "h2", repo="demo/fork"))
    assert [e.repo for e in ledger.entries()] == ["demo/up", "demo/fork"]
    assert [e.sha[0] for e in ledger.entries("demo/fork")] == ["b"]
    assert entry_to_json(ledger.entries("demo/fork")[0]) == {
        "fingerprint": fp("h2").patch,
        "repo": "demo/fork",
        "sha": "b" * 40,
        "subject": "fix b",
        "status": "claimed",
        "owner": None,
        "first_seen": NOW,
        "hunks": 1,
    }


def test_check_all_compares_with_the_ledger_and_earlier_candidates(
    ledger: Ledger, path: Path
) -> None:
    ledger.add(proposal("a", "h1", "h2"), owner="alice")
    before = path.read_bytes()
    verdicts = check_all(
        ledger,
        [
            proposal("b", "h1", "h2", repo="demo/fork"),  # the ledger has it
            proposal("c", "n1", "n2", "n3"),  # new
            proposal("d", "n1", "n2", "n3", repo="demo/fork"),  # same as c, this run
            proposal("e", "n1", "n2", "m9"),  # overlaps c
            Proposal("r", "f" * 40, "no hunks", None),
        ],
    )
    assert [v.status for v in verdicts] == [
        Status.DUPLICATE,
        Status.NEW,
        Status.DUPLICATE,
        Status.OVERLAP,
        Status.UNKNOWN,
    ]
    assert verdicts[2].matches[0].in_run
    assert verdicts[2].describe("d" * 40) == (
        "duplicate: same fix as demo/up cccccccccc (earlier in this run)"
    )
    assert verdicts[3].describe("e" * 40).endswith("(earlier in this run)")
    # Checking never writes to the file.
    assert path.read_bytes() == before
    assert len(ledger.entries()) == 1


def test_large_fingerprints_are_queried_in_chunks(ledger: Ledger) -> None:
    hunks = [f"h{i:04d}" for i in range(1200)]
    ledger.add(proposal("a", *hunks))
    verdict = ledger.verdict(fp(*hunks[:1100], "x"), 0.5)
    assert verdict.status is Status.OVERLAP
    assert verdict.matches[0].shared == 1100


def test_verdict_json() -> None:
    entry = ledger_module.Entry(1, "f", "r", "s" * 40, "fix", "claimed", "alice", NOW, 3)
    record = ledger_verdict_to_json(
        Verdict(Status.OVERLAP, (Match(entry, 2, 3, False), Match(entry, 3, 3, True, True)))
    )
    assert record["status"] == "overlap"
    first, second = record["matches"]
    assert first == {
        "source": "ledger",
        "repo": "r",
        "sha": "s" * 40,
        "subject": "fix",
        "status": "claimed",
        "owner": "alice",
        "first_seen": NOW,
        "exact": False,
        "shared": 2,
        "hunks": 3,
        "overlap": 0.6667,
    }
    assert (second["source"], second["status"], second["owner"]) == ("run", None, None)
    assert Match(entry, 0, 0, False).overlap == 0.0


# --- opening, versions and migrations ------------------------------------------------------


def test_other_files_are_refused(tmp_path: Path) -> None:
    other = tmp_path / "other.sqlite3"
    db = sqlite3.connect(other)
    db.execute("CREATE TABLE t (x)")
    db.commit()
    db.close()
    with pytest.raises(LedgerError, match="an SQLite file, but not a commitminer ledger"):
        open_ledger(other)
    db = sqlite3.connect(other)
    db.execute("PRAGMA application_id = 7")
    db.close()
    with pytest.raises(LedgerError, match="not a commitminer ledger"):
        open_ledger(other)
    text = tmp_path / "notes.txt"
    text.write_text("not a database, just text that is long enough to have a header\n" * 5)
    with pytest.raises(LedgerError, match="cannot use as a ledger: file is not a database"):
        open_ledger(text)
    with pytest.raises(LedgerError, match="is a directory"):
        open_ledger(tmp_path)
    with pytest.raises(LedgerError, match="cannot open"):
        open_ledger(tmp_path / "missing" / "dir" / "x.sqlite3")


def test_an_empty_file_becomes_a_ledger(tmp_path: Path) -> None:
    empty = tmp_path / "empty.sqlite3"
    empty.write_bytes(b"")
    with open_ledger(empty) as opened:
        assert opened.schema_version == SCHEMA_VERSION


def _set_version(path: Path, version: int) -> None:
    db = sqlite3.connect(path)
    db.execute(f"PRAGMA user_version = {version}")
    db.close()


NEXT = SCHEMA_VERSION + 1
"""A schema version this commitminer does not know yet, for migration tests."""


def _future(monkeypatch: pytest.MonkeyPatch, *steps: tuple[str, ...]) -> None:
    """Pretend the schema has ``len(steps)`` more versions, migrated by ``steps``."""
    monkeypatch.setattr(ledger_module, "SCHEMA_VERSION", SCHEMA_VERSION + len(steps))
    future = {SCHEMA_VERSION + i: step for i, step in enumerate(steps)}
    monkeypatch.setattr(ledger_module, "MIGRATIONS", {**ledger_module.MIGRATIONS, **future})


def test_a_newer_schema_is_refused(path: Path) -> None:
    open_ledger(path).close()
    _set_version(path, NEXT)
    with pytest.raises(LedgerError, match=f"schema version {NEXT} is newer than this commitminer"):
        open_ledger(path)


def test_older_schemas_are_migrated_forward(path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with open_ledger(path, now=clock) as opened:
        opened.add(proposal("a", "h1"))
    _future(monkeypatch, ("ALTER TABLE entries ADD COLUMN note TEXT",))
    with open_ledger(path) as migrated:
        assert migrated.schema_version == NEXT
        assert len(migrated.entries()) == 1
    db = sqlite3.connect(path)
    assert "note" in [row[1] for row in db.execute("PRAGMA table_info(entries)")]
    db.close()
    monkeypatch.setattr(ledger_module, "SCHEMA_VERSION", NEXT + 1)
    with pytest.raises(LedgerError, match=f"no migration from schema version {NEXT}"):
        open_ledger(path)


def test_a_failed_migration_leaves_the_file_unchanged(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    open_ledger(path).close()
    _future(monkeypatch, ("ALTER TABLE entries ADD COLUMN note TEXT",), ("ALTER TABLE nope ADD x",))
    with pytest.raises(LedgerError, match="cannot use as a ledger: no such table: nope"):
        open_ledger(path)
    # One transaction for the whole chain: the first step was rolled back with the second.
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert "note" not in [row[1] for row in db.execute("PRAGMA table_info(entries)")]
    db.close()
    monkeypatch.undo()
    with open_ledger(path) as opened:
        assert opened.schema_version == SCHEMA_VERSION


def _version_1_ledger(path: Path) -> None:
    """A ledger as schema version 1 created it (before watermarks), with one entry."""
    db = sqlite3.connect(path, isolation_level=None)
    for statement in ledger_module._TABLES_V1:
        db.execute(statement)
    db.execute("INSERT INTO meta VALUES ('fingerprint_version', ?)", (str(FINGERPRINT_VERSION),))
    db.execute(
        "INSERT INTO entries VALUES (1, ?, 'demo/up', ?, 'fix a', 'claimed', 'alice', ?, 1)",
        (fp("h1").patch, "a" * 40, NOW),
    )
    db.execute("INSERT INTO hunks VALUES (1, 'h1')")
    db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
    db.execute("PRAGMA user_version = 1")
    db.close()


def test_a_version_1_ledger_gains_watermarks_and_keeps_its_entries(path: Path) -> None:
    _version_1_ledger(path)
    with open_ledger(path, now=clock) as migrated:
        assert migrated.schema_version == 2
        assert [(e.sha[0], e.owner) for e in migrated.entries()] == [("a", "alice")]
        assert migrated.watermarks() == []
        assert migrated.verdict(fp("h1"), 0.5).status is Status.DUPLICATE
        migrated.record_run("demo/up", "clone", "c" * 40, ["c" * 40], [proposal("c", "h9")])
        assert migrated.watermark("demo/up", "clone") is not None


COUNTING_MIGRATION = (
    "INSERT INTO meta (key, value) VALUES ('migrations', '1') "
    "ON CONFLICT (key) DO UPDATE SET value = value + 1"
)
"""A migration that counts how often it ran (the value must stay '1')."""


def _migrations_applied(path: Path) -> tuple[int, str | None]:
    db = sqlite3.connect(path)
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    row = db.execute("SELECT value FROM meta WHERE key = 'migrations'").fetchone()
    db.close()
    return version, None if row is None else str(row[0])


def _race(monkeypatch: pytest.MonkeyPatch, between: Callable[[], None]) -> None:
    """Run ``between`` after the first unlocked look at a ledger, before the lock is taken."""
    original = ledger_module._identity
    calls = 0

    def looked(db: sqlite3.Connection) -> tuple[int, int]:
        nonlocal calls
        calls += 1
        result = original(db)
        if calls == 1:
            between()
        return result

    monkeypatch.setattr(ledger_module, "_identity", looked)


def test_a_ledger_created_by_another_process_after_the_first_look_is_used(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the emptiness check ran before the lock and saw the other's schema."""

    def other_process_creates_it() -> None:
        with open_ledger(path, now=clock) as other:
            other.add(proposal("a", "h1"), owner="other")

    _race(monkeypatch, other_process_creates_it)
    with open_ledger(path) as opened:
        assert opened.schema_version == SCHEMA_VERSION
        assert [e.owner for e in opened.entries()] == ["other"]
    db = sqlite3.connect(path)
    assert db.execute("SELECT count(*) FROM meta").fetchone()[0] == 1
    db.close()


def test_a_file_that_became_another_database_after_the_first_look_is_refused(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def other_process_writes_something_else() -> None:
        db = sqlite3.connect(path)
        db.execute("CREATE TABLE t (x)")
        db.execute("PRAGMA application_id = 7")
        db.commit()
        db.close()

    _race(monkeypatch, other_process_writes_something_else)
    with pytest.raises(LedgerError, match="an SQLite file, but not a commitminer ledger"):
        open_ledger(path)


def test_a_migration_done_by_another_process_after_the_first_look_is_not_repeated(
    path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: the version was read before the lock, so every opener migrated."""
    open_ledger(path).close()
    _future(monkeypatch, (COUNTING_MIGRATION,))
    _race(monkeypatch, lambda: open_ledger(path).close())
    with open_ledger(path) as opened:
        assert opened.schema_version == NEXT
    assert _migrations_applied(path) == (NEXT, "1")


def _open_from_another_process(
    path: str, migrate: bool, barrier: Any, results: Any
) -> None:  # pragma: no cover - runs in a child process
    from commitminer import ledger as module

    if migrate:
        module.MIGRATIONS[module.SCHEMA_VERSION] = (COUNTING_MIGRATION,)
        module.SCHEMA_VERSION += 1  # type: ignore[misc]
    barrier.wait()
    try:
        module.open_ledger(Path(path), timeout=30).close()
    except module.LedgerError as exc:
        results.put(f"error: {exc}")
    else:
        results.put("ok")


@pytest.mark.parametrize("migrate", [False, True], ids=["create", "migrate"])
def test_processes_opening_one_new_or_old_ledger_at_once_set_it_up_once(
    path: Path, migrate: bool
) -> None:
    if migrate:
        open_ledger(path).close()  # a ledger at today's version for everyone to migrate
    context = multiprocessing.get_context("spawn")
    count = 5
    barrier, results = context.Barrier(count), context.Queue()
    workers = [
        context.Process(
            target=_open_from_another_process, args=(str(path), migrate, barrier, results)
        )
        for _ in range(count)
    ]
    for worker in workers:
        worker.start()
    outcomes = [results.get(timeout=120) for _ in workers]
    for worker in workers:
        worker.join(timeout=120)
    assert outcomes == ["ok"] * count
    assert _migrations_applied(path) == ((NEXT, "1") if migrate else (SCHEMA_VERSION, None))


def test_fingerprint_versions_must_match(path: Path) -> None:
    open_ledger(path).close()
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value = '0'")
    db.commit()
    db.close()
    with pytest.raises(LedgerError, match="fingerprint version 0, this commitminer computes"):
        open_ledger(path)
    db = sqlite3.connect(path)
    db.execute("DELETE FROM meta")
    db.commit()
    db.close()
    with pytest.raises(LedgerError, match="fingerprint version none"):
        open_ledger(path)


def test_errors_inside_a_write_roll_back(ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(ledger, "_insert", broken)
    with pytest.raises(RuntimeError, match="disk on fire"):
        ledger.add(proposal("a", "h1"))
    monkeypatch.undo()
    assert ledger.add(proposal("a", "h1"))[0] is not None


def test_schema_creation_rolls_back_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ledger_module, "_SCHEMA", ("CREATE TABLE meta (x)", "NOT SQL"))
    with pytest.raises(LedgerError, match="syntax error"):
        open_ledger(tmp_path / "x.sqlite3")
    db = sqlite3.connect(tmp_path / "x.sqlite3")
    assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    db.close()


def test_utc_now_is_iso_utc() -> None:
    assert utc_now().endswith("+00:00")


# --- reading exported candidates -------------------------------------------------------------


def _line(**overrides: object) -> str:
    record: dict[str, object] = {
        "schema_version": 3,
        "rank": 1,
        "repo": "r",
        "sha": "a" * 40,
        "subject": "fix",
        "fingerprint": {"version": FINGERPRINT_VERSION, "patch": fp("h1").patch, "hunks": ["h1"]},
    }
    record.update(overrides)
    return json.dumps(record)


def test_read_candidates(tmp_path: Path) -> None:
    path = tmp_path / "c.jsonl"
    run = json.dumps({"kind": "run", "schema_version": 5, "walked": 3})
    path.write_text(
        run + "\n" + _line(kind="candidate", schema_version=5) + "\n\n"
        + _line(rank=None, fingerprint=None) + "\n"
    )  # fmt: skip
    first, second = read_candidates(path)
    assert first.rank == 1
    assert first.proposal == Proposal("r", "a" * 40, "fix", fp("h1"))
    assert second.rank is None
    assert second.proposal.fingerprint is None


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("{", "invalid JSON"),
        ("[]", "expected an object"),
        (_line(schema_version=2), "schema_version 2, expected 3 to 6"),
        (_line(schema_version=7), "schema_version 7, expected 3 to 6"),
        (_line(rank="1"), "rank: expected int"),
        (_line(repo=None), "repo: expected str"),
        (_line(fingerprint=[]), "fingerprint: expected an object or null"),
        (_line(fingerprint={"version": 1}), "fingerprint: version 1, expected 2"),
        (_line(fingerprint={"version": 2, "hunks": []}), "non-empty list of strings"),
        (
            _line(fingerprint={"version": 2, "patch": "x", "hunks": ["h1"]}),
            "the patch hash does not match its hunks",
        ),
    ],
)
def test_read_candidates_rejects_bad_lines(tmp_path: Path, text: str, message: str) -> None:
    path = tmp_path / "c.jsonl"
    path.write_text(text + "\n")
    with pytest.raises(LedgerError, match=message):
        read_candidates(path)


def test_read_candidates_reports_unreadable_files(tmp_path: Path) -> None:
    with pytest.raises(LedgerError, match="cannot read"):
        read_candidates(tmp_path / "missing.jsonl")


def test_the_unique_constraint_stops_a_writer_that_skips_the_check(
    ledger: Ledger, monkeypatch: pytest.MonkeyPatch
) -> None:
    ledger.add(proposal("a", "h1", "h2"), owner="alice")
    # A writer that does not look first still cannot record the same fix twice.
    monkeypatch.setattr(ledger, "verdict", lambda *args: Verdict(Status.NEW))
    entry, verdict = ledger.add(proposal("b", "h2", "h1", repo="demo/fork"))
    assert entry is None
    assert verdict.status is Status.DUPLICATE
    assert verdict.matches[0].exact
    assert verdict.matches[0].entry.owner == "alice"


def test_creating_an_existing_ledger_again_changes_nothing(path: Path) -> None:
    open_ledger(path).close()
    db = sqlite3.connect(path, isolation_level=None)
    ledger_module._setup(db, str(path))  # as when another process won the race to create it
    assert db.execute("SELECT count(*) FROM meta").fetchone()[0] == 1
    db.close()


def test_a_missing_ledger_is_only_created_on_request(path: Path) -> None:
    with pytest.raises(LedgerError, match="no such ledger \\(ledger add creates one\\)"):
        open_ledger(path, create=False)
    assert not path.exists()
    open_ledger(path).close()
    with open_ledger(path, create=False) as opened:
        assert opened.schema_version == SCHEMA_VERSION


def test_sqlite_errors_after_opening_are_ledger_errors(path: Path, tmp_path: Path) -> None:
    with open_ledger(path, now=clock) as opened:
        opened.add(proposal("a", "h1"))
    # Another writer holds the lock longer than the timeout.
    other = sqlite3.connect(path, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    try:
        with (
            open_ledger(path, timeout=0.05) as mine,
            pytest.raises(LedgerError, match=f"{path}: cannot write: database is locked"),
        ):
            mine.add(proposal("b", "h2"))
    finally:
        other.execute("ROLLBACK")
        other.close()
    # A closed connection: every read and write reports the file, not a traceback.
    closed = open_ledger(path)
    closed.close()
    with pytest.raises(LedgerError, match=f"{path}: cannot read: Cannot operate on a closed"):
        closed.entries()
    with pytest.raises(LedgerError, match="cannot read"):
        _ = closed.schema_version
    with pytest.raises(LedgerError, match="cannot read"):
        check_all(closed, [proposal("d", "h4")])
    with pytest.raises(LedgerError, match="cannot write"):
        closed.add(proposal("d", "h4"))
    # A read-only file can be read and checked, not written.
    read_only = tmp_path / "ro.sqlite3"
    read_only.write_bytes(path.read_bytes())
    read_only.chmod(0o444)
    if os.access(read_only, os.W_OK):
        pytest.skip("file permissions are not enforced here (running as root?)")
    with open_ledger(read_only) as frozen:
        assert len(frozen.entries()) == 1
        assert check_all(frozen, [proposal("b", "h1")])[0].status is Status.DUPLICATE
        with pytest.raises(LedgerError, match="cannot write: attempt to write a readonly"):
            frozen.add(proposal("e", "h5"))
