"""Helpers for the export files the CLI tests write and read back."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def read_export(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """The run record and the candidate records of a ``mine --out`` or ``prs --out`` file."""
    run, *candidates = (json.loads(line) for line in path.read_text().splitlines())
    assert run["kind"] == "run", run
    assert all(record["kind"] == "candidate" for record in candidates)
    return run, candidates
