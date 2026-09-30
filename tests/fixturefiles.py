"""Helpers that write GitHub API fixtures in the recorded format, and a fake clock."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from commitminer.fixtures import FIXTURE_FORMAT, FIXTURE_VERSION, fixture_name

API = "https://api.github.com"


def reply(
    status: int = 200, body: Any = None, headers: dict[str, str] | None = None
) -> dict[str, Any]:
    """One recorded response."""
    entry: dict[str, Any] = {"status": status, "headers": headers or {}}
    if body is not None:
        entry["json"] = body
    return entry


def write_fixture(directory: Path, key: str, *responses: dict[str, Any]) -> Path:
    """Write the fixture file for ``key`` (such as ``GET /repos/o/r/pulls?per_page=100``)."""
    directory.mkdir(parents=True, exist_ok=True)
    record = {
        "format": FIXTURE_FORMAT,
        "version": FIXTURE_VERSION,
        "key": key,
        "responses": list(responses),
    }
    path = directory / fixture_name(key)
    path.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n", encoding="ascii")
    return path


class FakeClock:
    """A clock that only moves when something sleeps."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
