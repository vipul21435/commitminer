"""Recorded history files: write walked commits once, replay them offline.

A recording is JSON Lines. The first line is a header; every other line is one
commit, newest first, exactly as the walker produced it::

    {"format": "commitminer-history", "version": 1, "repo": ..., "url": ..., "head": ...,
     "commits": 312}
    {"sha": ..., "parents": [...], "date": ..., "message": ..., "files": [...]}

A file entry has ``path``, ``added``, ``deleted`` and, only when set,
``old_path`` (renames), ``signals`` (content signals read at walk time) and
``patch`` (patch measurements, see :class:`~commitminer.models.PatchStats`).
To keep recordings small, ``patch`` always has ``hunks`` but leaves out every
other field that has its default: ``code_hunks`` equal to ``hunks``,
``code_added`` and ``code_deleted`` equal to the file's ``added`` and
``deleted``, zero counts, an empty ``api`` and an empty ``tests``.
``hunk_hashes`` is written whenever it was computed, even when empty, because
its absence means "not computed". Recordings made before patches were read
have no ``patch``, and those made before fingerprints have no ``hunk_hashes``;
both still load.

Replaying a recording yields the same :class:`~commitminer.models.Commit` objects
as walking the clone, so everything after parsing runs the same code path.
Output is deterministic (sorted keys, no timestamps), so re-recording the same
revision produces a byte-identical file. A path ending in ``.gz`` is gzipped
(with a zero timestamp), which keeps large histories small enough to commit.
"""

from __future__ import annotations

import gzip
import io
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from commitminer.models import Commit, FileChange, PatchStats

FORMAT = "commitminer-history"
VERSION = 1


class HistoryError(ValueError):
    """A recorded history file is malformed or has an unsupported version."""


@dataclass(frozen=True, slots=True)
class HistoryHeader:
    """Provenance of a recording: which repository and revision it came from."""

    repo: str
    url: str | None
    head: str | None
    commits: int


def _patch_to_json(change: FileChange, patch: PatchStats) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "code_hunks": patch.hunks,
        "code_added": change.added,
        "code_deleted": change.deleted,
        "test_added": 0,
        "test_deleted": 0,
        "asserts": 0,
        "api": [],
        "tests": [],
    }
    values: dict[str, Any] = {
        "code_hunks": patch.code_hunks,
        "code_added": patch.code_added,
        "code_deleted": patch.code_deleted,
        "test_added": patch.test_added,
        "test_deleted": patch.test_deleted,
        "asserts": patch.asserts,
        "api": list(patch.api),
        "tests": list(patch.tests),
    }
    record: dict[str, Any] = {"hunks": patch.hunks}
    record.update((key, value) for key, value in values.items() if value != defaults[key])
    if patch.hunk_hashes is not None:
        record["hunk_hashes"] = list(patch.hunk_hashes)
    return record


def _file_to_json(change: FileChange) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": change.path,
        "added": change.added,
        "deleted": change.deleted,
    }
    if change.old_path is not None:
        record["old_path"] = change.old_path
    if change.signals:
        record["signals"] = list(change.signals)
    if change.patch is not None:
        record["patch"] = _patch_to_json(change, change.patch)
    return record


def commit_to_json(commit: Commit) -> dict[str, Any]:
    """Convert a commit into the JSON object stored on one recording line."""
    return {
        "sha": commit.sha,
        "parents": list(commit.parents),
        "date": commit.date,
        "message": commit.message,
        "files": [_file_to_json(f) for f in commit.files],
    }


def _optional_int(value: Any, where: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoryError(f"{where}: expected a non-negative integer or null, got {value!r}")
    return value


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise HistoryError(f"{where}: expected a string, got {value!r}")
    return value


_PATCH_KEYS = frozenset(
    {"hunks", "code_hunks", "code_added", "code_deleted", "test_added", "test_deleted"}
    | {"asserts", "api", "hunk_hashes", "tests"}
)


def _count(raw: dict[str, Any], key: str, default: int | None, where: str) -> int:
    value = _optional_int(raw.get(key, default), f"{where}.{key}")
    if value is None:
        raise HistoryError(f"{where}.{key}: required")
    return value


def _patch_from_json(raw: Any, added: int | None, deleted: int | None, where: str) -> PatchStats:
    if not isinstance(raw, dict):
        raise HistoryError(f"{where}: expected an object")
    unknown = sorted(set(raw) - _PATCH_KEYS)
    if unknown:
        raise HistoryError(f"{where}: unknown key {unknown[0]!r}")
    if added is None or deleted is None:
        raise HistoryError(f"{where}: a binary file has no patch")
    api = raw.get("api", [])
    if not isinstance(api, list):
        raise HistoryError(f"{where}.api: expected a list")
    tests = raw.get("tests", [])
    if not isinstance(tests, list):
        raise HistoryError(f"{where}.tests: expected a list")
    hashes = raw.get("hunk_hashes")
    if hashes is not None and not isinstance(hashes, list):
        raise HistoryError(f"{where}.hunk_hashes: expected a list")
    hunks = _count(raw, "hunks", None, where)
    return PatchStats(
        hunks=hunks,
        code_hunks=_count(raw, "code_hunks", hunks, where),
        code_added=_count(raw, "code_added", added, where),
        code_deleted=_count(raw, "code_deleted", deleted, where),
        test_added=_count(raw, "test_added", 0, where),
        test_deleted=_count(raw, "test_deleted", 0, where),
        asserts=_count(raw, "asserts", 0, where),
        api=tuple(_string(name, f"{where}.api") for name in api),
        hunk_hashes=None
        if hashes is None
        else tuple(_string(item, f"{where}.hunk_hashes") for item in hashes),
        tests=tuple(_string(name, f"{where}.tests") for name in tests),
    )


def commit_from_json(record: Any, where: str = "commit") -> Commit:
    """Validate and convert one recording line back into a :class:`Commit`."""
    if not isinstance(record, dict):
        raise HistoryError(f"{where}: expected an object")
    try:
        parents = record["parents"]
        raw_files = record["files"]
        sha = _string(record["sha"], f"{where}.sha")
        date = _string(record["date"], f"{where}.date")
        message = _string(record["message"], f"{where}.message")
    except KeyError as exc:
        raise HistoryError(f"{where}: missing field {exc.args[0]!r}") from exc
    if not isinstance(parents, list) or not isinstance(raw_files, list):
        raise HistoryError(f"{where}: parents and files must be lists")
    files: list[FileChange] = []
    for index, raw in enumerate(raw_files):
        at = f"{where}.files[{index}]"
        if not isinstance(raw, dict) or "path" not in raw:
            raise HistoryError(f"{at}: expected an object with a path")
        old_path = raw.get("old_path")
        signals = raw.get("signals", [])
        if not isinstance(signals, list):
            raise HistoryError(f"{at}.signals: expected a list")
        added = _optional_int(raw.get("added"), f"{at}.added")
        deleted = _optional_int(raw.get("deleted"), f"{at}.deleted")
        patch = raw.get("patch")
        files.append(
            FileChange(
                path=_string(raw["path"], f"{at}.path"),
                added=added,
                deleted=deleted,
                old_path=None if old_path is None else _string(old_path, f"{at}.old_path"),
                signals=tuple(_string(s, f"{at}.signals") for s in signals),
                patch=None
                if patch is None
                else _patch_from_json(patch, added, deleted, f"{at}.patch"),
            )
        )
    return Commit(
        sha=sha,
        parents=tuple(_string(p, f"{where}.parents") for p in parents),
        date=date,
        message=message,
        files=tuple(files),
    )


def _dumps(obj: dict[str, Any]) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def write_history(
    path: Path,
    commits: Sequence[Commit],
    repo: str,
    url: str | None = None,
    head: str | None = None,
) -> HistoryHeader:
    """Write ``commits`` to ``path`` as a recording and return its header."""
    header = HistoryHeader(repo=repo, url=url, head=head, commits=len(commits))
    lines = [
        _dumps(
            {
                "format": FORMAT,
                "version": VERSION,
                "repo": repo,
                "url": url,
                "head": head,
                "commits": len(commits),
            }
        )
    ]
    lines += [_dumps(commit_to_json(c)) for c in commits]
    path.parent.mkdir(parents=True, exist_ok=True)
    data = ("\n".join(lines) + "\n").encode("ascii")
    if path.suffix == ".gz":
        # mtime=0 and no embedded file name keep the compressed bytes reproducible.
        buffer = io.BytesIO()
        with gzip.GzipFile(filename="", mode="wb", fileobj=buffer, mtime=0) as handle:
            handle.write(data)
        data = buffer.getvalue()
    path.write_bytes(data)
    return header


def _parse_header(line: str) -> HistoryHeader:
    try:
        raw = json.loads(line)
    except json.JSONDecodeError as exc:
        raise HistoryError(f"line 1: invalid JSON: {exc.msg}") from exc
    if not isinstance(raw, dict) or raw.get("format") != FORMAT:
        raise HistoryError(f"line 1: not a {FORMAT} file")
    if raw.get("version") != VERSION:
        raise HistoryError(f"line 1: unsupported version {raw.get('version')!r}")
    url, head, count = raw.get("url"), raw.get("head"), raw.get("commits")
    if url is not None and not isinstance(url, str):
        raise HistoryError("line 1: url must be a string or null")
    if head is not None and not isinstance(head, str):
        raise HistoryError("line 1: head must be a string or null")
    commits = _optional_int(count, "line 1: commits")
    if commits is None:
        raise HistoryError("line 1: commits is required")
    return HistoryHeader(
        repo=_string(raw.get("repo"), "line 1: repo"), url=url, head=head, commits=commits
    )


def parse_history(lines: Iterable[str]) -> tuple[HistoryHeader, list[Commit]]:
    """Parse recording lines; the commit count must match the header."""
    iterator = iter(lines)
    first = next(iterator, None)
    if first is None or not first.strip():
        raise HistoryError("empty history file")
    header = _parse_header(first)
    commits: list[Commit] = []
    for number, line in enumerate(iterator, start=2):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise HistoryError(f"line {number}: invalid JSON: {exc.msg}") from exc
        commits.append(commit_from_json(record, f"line {number}"))
    if len(commits) != header.commits:
        raise HistoryError(f"header says {header.commits} commits but the file has {len(commits)}")
    return header, commits


def read_history(path: Path) -> tuple[HistoryHeader, list[Commit]]:
    """Read a recording written by :func:`write_history` (gzipped if it ends in ``.gz``)."""
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            return parse_history(handle)
    with path.open(encoding="utf-8") as handle:
        return parse_history(handle)
