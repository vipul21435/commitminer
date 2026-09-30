"""A SQLite ledger of proposed fixes, so the same fix is never proposed twice.

One file, standard library only (:mod:`sqlite3`), no server: it can sit on a
shared drive or travel between CI runs. Each entry is one fix, identified by
its patch fingerprint (see :mod:`commitminer.fingerprint`)::

    PRAGMA application_id = 0x434D4C47   -- "CMLG": other SQLite files are refused
    PRAGMA user_version = 1              -- the schema version, migrated forward on open

    meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)      -- fingerprint_version
    entries(id INTEGER PRIMARY KEY,
            fingerprint TEXT NOT NULL UNIQUE,            -- the claim: one row per fix
            repo TEXT NOT NULL, sha TEXT NOT NULL, subject TEXT NOT NULL,
            status TEXT NOT NULL,                        -- proposed or claimed
            owner TEXT, first_seen TEXT NOT NULL,        -- UTC, ISO 8601
            hunk_count INTEGER NOT NULL,                 -- distinct hunk hashes
            UNIQUE (repo, sha))
    hunks(entry_id INTEGER NOT NULL REFERENCES entries(id), hash TEXT NOT NULL,
          PRIMARY KEY (entry_id, hash)) WITHOUT ROWID    -- indexed by hash

A candidate checked against the ledger is a **duplicate** when an entry has the
same patch fingerprint (a cherry-pick, a fork, a re-indented or moved copy), an
**overlap** when it shares at least ``min_overlap`` of the distinct hunks of the
smaller of the two (a cherry-pick with a resolved conflict, a squash), **new**
otherwise, and **unknown** when it has no fingerprint.

Adding runs in one ``BEGIN IMMEDIATE`` transaction: check, then insert. The
unique constraint on the fingerprint makes the claim atomic even against a
writer that skips the check, so of two authors adding the same fix at the same
time exactly one succeeds. Checking never writes: :func:`check_all` copies the
ledger into memory and adds each checked candidate to the copy, so candidates
of one run are also compared with each other.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Final

from commitminer.fingerprint import FINGERPRINT_VERSION, Fingerprint

APPLICATION_ID: Final = 0x434D4C47
SCHEMA_VERSION: Final = 1
DEFAULT_MIN_OVERLAP: Final = 0.5
STATUSES: Final = ("proposed", "claimed")
"""Entry statuses: recorded as proposed to authors, or taken by an author."""

_SCHEMA: Final = (
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE entries (
        id INTEGER PRIMARY KEY,
        fingerprint TEXT NOT NULL UNIQUE,
        repo TEXT NOT NULL,
        sha TEXT NOT NULL,
        subject TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('proposed', 'claimed')),
        owner TEXT,
        first_seen TEXT NOT NULL,
        hunk_count INTEGER NOT NULL CHECK (hunk_count > 0),
        UNIQUE (repo, sha)
    )""",
    """CREATE TABLE hunks (
        entry_id INTEGER NOT NULL REFERENCES entries (id) ON DELETE CASCADE,
        hash TEXT NOT NULL,
        PRIMARY KEY (entry_id, hash)
    ) WITHOUT ROWID""",
    "CREATE INDEX hunks_by_hash ON hunks (hash)",
)
MIGRATIONS: dict[int, tuple[str, ...]] = {}
"""Statements that take the schema from version ``k`` to ``k + 1``, keyed by ``k``."""

_ENTRY_COLUMNS: Final = "id, fingerprint, repo, sha, subject, status, owner, first_seen, hunk_count"
_CHUNK: Final = 500
"""Hunk hashes per query, well below SQLite's limit on bound parameters."""


class LedgerError(RuntimeError):
    """The ledger cannot be opened or read, or a candidate cannot be recorded."""


def utc_now() -> str:
    """The current time in UTC, to the second, as ISO 8601."""
    return datetime.now(UTC).isoformat(timespec="seconds")


@dataclass(frozen=True, slots=True)
class Entry:
    """One recorded fix."""

    id: int
    fingerprint: str
    repo: str
    sha: str
    subject: str
    status: str
    owner: str | None
    first_seen: str
    hunk_count: int


@dataclass(frozen=True, slots=True)
class Proposal:
    """A candidate to check or add: where it comes from and its fingerprint."""

    repo: str
    sha: str
    subject: str
    fingerprint: Fingerprint | None


@dataclass(frozen=True, slots=True)
class Match:
    """A ledger entry that a candidate duplicates or overlaps."""

    entry: Entry
    shared: int
    """Distinct hunk hashes both have."""
    smaller: int
    """Distinct hunk hashes of the smaller of the two."""
    exact: bool
    """Same patch fingerprint."""
    in_run: bool = False
    """The entry is an earlier candidate of the same run, not a recorded one."""

    @property
    def overlap(self) -> float:
        """``shared / smaller``: 1.0 for an exact match."""
        return self.shared / self.smaller if self.smaller else 0.0


class Status(StrEnum):
    """The ledger verdict on one candidate."""

    NEW = "new"
    DUPLICATE = "duplicate"
    OVERLAP = "overlap"
    UNKNOWN = "unknown"


def _who(entry: Entry, in_run: bool) -> str:
    if in_run:
        return "earlier in this run"
    owner = f" by {entry.owner}" if entry.owner else ""
    return f"{entry.status}{owner} on {entry.first_seen[:10]}"


@dataclass(frozen=True, slots=True)
class Verdict:
    """What the ledger says about one candidate, with the entries it matched."""

    status: Status
    matches: tuple[Match, ...] = ()

    def describe(self, sha: str) -> str:
        """One line: the status and the best match."""
        if self.status is Status.NEW:
            return "new"
        if self.status is Status.UNKNOWN:
            return "unknown: no fingerprint (no hunk hashes for its source and test changes)"
        best = self.matches[0]
        entry, where = best.entry, f"{best.entry.repo} {best.entry.sha[:10]}"
        more = len(self.matches) - 1
        extra = f" (+{more} more)" if more else ""
        if self.status is Status.DUPLICATE and entry.sha == sha and not best.in_run:
            return f"duplicate: already in the ledger ({_who(entry, False)}){extra}"
        if self.status is Status.DUPLICATE:
            return f"duplicate: same fix as {where} ({_who(entry, best.in_run)}){extra}"
        return (
            f"overlap: {best.shared} of {best.smaller} hunks shared with {where} "
            f"({_who(entry, best.in_run)}){extra}"
        )


def _entry(row: Sequence[Any]) -> Entry:
    return Entry(*row)


def _check(value: Any, kind: type, where: str) -> Any:
    if not isinstance(value, kind) or isinstance(value, bool):
        raise LedgerError(f"{where}: expected {kind.__name__}, got {value!r}")
    return value


class Ledger:
    """An open ledger (a file, or an in-memory copy made by :meth:`snapshot`)."""

    def __init__(
        self, db: sqlite3.Connection, path: Path | None, now: Callable[[], str] = utc_now
    ) -> None:
        self._db = db
        self.path = path
        self._now = now
        self._run: set[int] = set()

    def __enter__(self) -> Ledger:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection."""
        self._db.close()

    @property
    def schema_version(self) -> int:
        """``PRAGMA user_version``."""
        return int(self._db.execute("PRAGMA user_version").fetchone()[0])

    def entries(self, repo: str | None = None) -> list[Entry]:
        """Recorded entries, oldest first (optionally of one repository)."""
        query = f"SELECT {_ENTRY_COLUMNS} FROM entries"
        rows = (
            self._db.execute(query + " ORDER BY first_seen, id")
            if repo is None
            else self._db.execute(query + " WHERE repo = ? ORDER BY first_seen, id", (repo,))
        )
        return [_entry(row) for row in rows]

    def _exact(self, fingerprint: str) -> Entry | None:
        row = self._db.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return None if row is None else _entry(row)

    def _shared(self, hunks: Sequence[str]) -> dict[int, int]:
        counts: dict[int, int] = {}
        for start in range(0, len(hunks), _CHUNK):
            chunk = hunks[start : start + _CHUNK]
            marks = ",".join("?" * len(chunk))
            query = (
                f"SELECT entry_id, COUNT(*) FROM hunks WHERE hash IN ({marks}) GROUP BY entry_id"
            )
            for entry_id, count in self._db.execute(query, chunk):
                counts[entry_id] = counts.get(entry_id, 0) + count
        return counts

    def _by_id(self, entry_id: int) -> Entry:
        row = self._db.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE id = ?", (entry_id,)
        ).fetchone()
        return _entry(row)

    def verdict(self, fingerprint: Fingerprint | None, min_overlap: float | None) -> Verdict:
        """Compare a fingerprint with the entries; ``min_overlap=None`` checks exact only."""
        if fingerprint is None:
            return Verdict(Status.UNKNOWN)
        matches: list[Match] = []
        exact = self._exact(fingerprint.patch)
        if exact is not None:
            count = exact.hunk_count
            matches.append(Match(exact, count, count, True, exact.id in self._run))
        if min_overlap is not None:
            distinct = sorted(fingerprint.distinct)
            for entry_id, shared in self._shared(distinct).items():
                if exact is not None and entry_id == exact.id:
                    continue
                entry = self._by_id(entry_id)
                smaller = min(len(distinct), entry.hunk_count)
                match = Match(entry, shared, smaller, False, entry_id in self._run)
                if match.overlap >= min_overlap:
                    matches.append(match)
        matches.sort(key=lambda m: (not m.exact, -m.overlap, -m.shared, m.entry.id))
        if exact is not None:
            return Verdict(Status.DUPLICATE, tuple(matches))
        if matches:
            return Verdict(Status.OVERLAP, tuple(matches))
        return Verdict(Status.NEW)

    def _insert(self, proposal: Proposal, status: str, owner: str | None) -> Entry:
        fingerprint = proposal.fingerprint
        assert fingerprint is not None
        distinct = sorted(fingerprint.distinct)
        cursor = self._db.execute(
            "INSERT INTO entries (fingerprint, repo, sha, subject, status, owner, first_seen, "
            "hunk_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                fingerprint.patch,
                proposal.repo,
                proposal.sha,
                proposal.subject,
                status,
                owner,
                self._now(),
                len(distinct),
            ),
        )
        entry_id = cursor.lastrowid
        assert entry_id is not None
        self._db.executemany(
            "INSERT INTO hunks (entry_id, hash) VALUES (?, ?)", ((entry_id, h) for h in distinct)
        )
        return self._by_id(entry_id)

    def _holder(self, proposal: Proposal) -> Entry:
        """The entry that blocks ``proposal``: same fingerprint, or same repo and sha."""
        assert proposal.fingerprint is not None
        found = self._exact(proposal.fingerprint.patch)
        if found is not None:
            return found
        row = self._db.execute(
            f"SELECT {_ENTRY_COLUMNS} FROM entries WHERE repo = ? AND sha = ?",
            (proposal.repo, proposal.sha),
        ).fetchone()
        return _entry(row)

    def add(
        self,
        proposal: Proposal,
        *,
        status: str = "claimed",
        owner: str | None = None,
        min_overlap: float | None = DEFAULT_MIN_OVERLAP,
    ) -> tuple[Entry | None, Verdict]:
        """Record ``proposal`` unless it duplicates (or, with ``min_overlap``, overlaps) an entry.

        Returns the new entry and the verdict it was added under, or ``None``
        and the verdict that refused it. The check and the insert are one
        transaction; the unique constraints refuse the fix if another writer got
        there first.
        """
        if status not in STATUSES:
            raise LedgerError(f"unknown status {status!r} ({', '.join(STATUSES)})")
        if proposal.fingerprint is None:
            return None, Verdict(Status.UNKNOWN)
        self._db.execute("BEGIN IMMEDIATE")
        try:
            verdict = self.verdict(proposal.fingerprint, min_overlap)
            if verdict.status is not Status.NEW:
                self._db.execute("ROLLBACK")
                return None, verdict
            entry = self._insert(proposal, status, owner)
            self._db.execute("COMMIT")
        except sqlite3.IntegrityError:
            self._db.execute("ROLLBACK")
            holder = self._holder(proposal)
            exact = holder.fingerprint == proposal.fingerprint.patch
            count = holder.hunk_count
            return None, Verdict(Status.DUPLICATE, (Match(holder, count, count, exact),))
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        return entry, verdict

    def snapshot(self) -> Ledger:
        """An in-memory copy to check candidates against without writing to the file."""
        memory = sqlite3.connect(":memory:", isolation_level=None)
        self._db.backup(memory)
        return Ledger(memory, None, self._now)

    def note(self, proposal: Proposal) -> None:
        """Add a checked candidate to this (in-memory) copy, marked as part of the run."""
        if proposal.fingerprint is None:
            return
        try:
            entry = self._insert(proposal, "proposed", None)
        except sqlite3.IntegrityError:
            return  # the same fix, or the same commit, is already there
        self._run.add(entry.id)


def check_all(
    ledger: Ledger, proposals: Iterable[Proposal], min_overlap: float = DEFAULT_MIN_OVERLAP
) -> list[Verdict]:
    """Verdicts for ``proposals``, each compared with the ledger and the proposals before it."""
    scratch = ledger.snapshot()
    try:
        verdicts = []
        for proposal in proposals:
            verdicts.append(scratch.verdict(proposal.fingerprint, min_overlap))
            scratch.note(proposal)
        return verdicts
    finally:
        scratch.close()


def _create(db: sqlite3.Connection) -> None:
    db.execute("BEGIN IMMEDIATE")
    try:
        # Another process may have created it between our look and the lock.
        if db.execute("PRAGMA application_id").fetchone()[0] == 0:
            for statement in _SCHEMA:
                db.execute(statement)
            db.execute(
                "INSERT INTO meta (key, value) VALUES ('fingerprint_version', ?)",
                (str(FINGERPRINT_VERSION),),
            )
            db.execute(f"PRAGMA application_id = {APPLICATION_ID}")
            db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


def _migrate(db: sqlite3.Connection, version: int) -> None:
    while version < SCHEMA_VERSION:
        steps = MIGRATIONS.get(version)
        if steps is None:
            raise LedgerError(f"no migration from schema version {version}")
        db.execute("BEGIN IMMEDIATE")
        try:
            for statement in steps:
                db.execute(statement)
            db.execute(f"PRAGMA user_version = {version + 1}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        version += 1


def _prepare(db: sqlite3.Connection, where: str) -> None:
    db.execute("PRAGMA foreign_keys = ON")
    application = db.execute("PRAGMA application_id").fetchone()[0]
    if application == 0:
        if db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0]:
            raise LedgerError(f"{where}: an SQLite file, but not a commitminer ledger")
        _create(db)
    elif application != APPLICATION_ID:
        raise LedgerError(f"{where}: an SQLite file, but not a commitminer ledger")
    version = int(db.execute("PRAGMA user_version").fetchone()[0])
    if version > SCHEMA_VERSION:
        raise LedgerError(
            f"{where}: schema version {version} is newer than this commitminer "
            f"supports ({SCHEMA_VERSION}); upgrade commitminer"
        )
    _migrate(db, version)
    row = db.execute("SELECT value FROM meta WHERE key = 'fingerprint_version'").fetchone()
    if row is None or row[0] != str(FINGERPRINT_VERSION):
        found = "none" if row is None else row[0]
        raise LedgerError(
            f"{where}: fingerprint version {found}, this commitminer computes "
            f"version {FINGERPRINT_VERSION}; fingerprints of two versions never match"
        )


def open_ledger(
    path: Path, *, timeout: float = 10.0, now: Callable[[], str] | None = None
) -> Ledger:
    """Open the ledger at ``path``, creating it (and its schema) if it does not exist.

    ``timeout`` is how long a write waits for another writer's lock; ``now``
    gives the ``first_seen`` time of new entries (default :func:`utc_now`).
    """
    if path.is_dir():
        raise LedgerError(f"{path}: is a directory")
    try:
        db = sqlite3.connect(path, timeout=timeout, isolation_level=None)
    except sqlite3.Error as exc:
        raise LedgerError(f"{path}: cannot open: {exc}") from exc
    try:
        _prepare(db, str(path))
    except sqlite3.DatabaseError as exc:
        db.close()
        raise LedgerError(f"{path}: cannot use as a ledger: {exc}") from exc
    except BaseException:
        db.close()
        raise
    return Ledger(db, path, now if now is not None else utc_now)


def _fingerprint_from_json(raw: Any, where: str) -> Fingerprint | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise LedgerError(f"{where}.fingerprint: expected an object or null")
    version = raw.get("version")
    if version != FINGERPRINT_VERSION:
        raise LedgerError(
            f"{where}.fingerprint: version {version!r}, expected {FINGERPRINT_VERSION} "
            "(mine the candidates again)"
        )
    hunks = raw.get("hunks")
    if not isinstance(hunks, list) or not hunks or not all(isinstance(h, str) for h in hunks):
        raise LedgerError(f"{where}.fingerprint.hunks: expected a non-empty list of strings")
    value = Fingerprint.of(hunks)
    if value.patch != raw.get("patch"):
        raise LedgerError(f"{where}.fingerprint: the patch hash does not match its hunks")
    return value


@dataclass(frozen=True, slots=True)
class RankedProposal:
    """A candidate read from an exported JSONL file, with its rank there."""

    rank: int | None
    proposal: Proposal


READABLE_SCHEMAS: Final = (3, 4)
"""Export schema versions whose candidates the ledger can read (4 only adds ``pull_request``)."""


def read_candidates(path: Path) -> list[RankedProposal]:
    """Read the candidates of a ``mine --out`` or ``prs --out`` file (schema version 3 or 4)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LedgerError(f"{path}: cannot read: {exc}") from exc
    found: list[RankedProposal] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        where = f"{path}:{number}"
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise LedgerError(f"{where}: invalid JSON: {exc.msg}") from exc
        if not isinstance(record, dict):
            raise LedgerError(f"{where}: expected an object")
        if record.get("schema_version") not in READABLE_SCHEMAS:
            raise LedgerError(
                f"{where}: schema_version {record.get('schema_version')!r}, expected 3 or 4 "
                "(mine the candidates again)"
            )
        rank = record.get("rank")
        found.append(
            RankedProposal(
                None if rank is None else _check(rank, int, f"{where}.rank"),
                Proposal(
                    _check(record.get("repo"), str, f"{where}.repo"),
                    _check(record.get("sha"), str, f"{where}.sha"),
                    _check(record.get("subject"), str, f"{where}.subject"),
                    _fingerprint_from_json(record.get("fingerprint"), where),
                ),
            )
        )
    return found


def entry_to_json(entry: Entry) -> dict[str, Any]:
    """One entry as printed by ``ledger list --json``."""
    return {
        "fingerprint": entry.fingerprint,
        "repo": entry.repo,
        "sha": entry.sha,
        "subject": entry.subject,
        "status": entry.status,
        "owner": entry.owner,
        "first_seen": entry.first_seen,
        "hunks": entry.hunk_count,
    }


def ledger_verdict_to_json(verdict: Verdict) -> dict[str, Any]:
    """The ``ledger`` object of the export: status and matches."""
    return {
        "status": verdict.status.value,
        "matches": [
            {
                "source": "run" if match.in_run else "ledger",
                "repo": match.entry.repo,
                "sha": match.entry.sha,
                "subject": match.entry.subject,
                "status": None if match.in_run else match.entry.status,
                "owner": None if match.in_run else match.entry.owner,
                "first_seen": None if match.in_run else match.entry.first_seen,
                "exact": match.exact,
                "shared": match.shared,
                "hunks": match.entry.hunk_count,
                "overlap": round(match.overlap, 4),
            }
            for match in verdict.matches
        ],
    }
