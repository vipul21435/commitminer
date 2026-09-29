"""Candidate export (JSON Lines) and plain-text rendering for the terminal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from commitminer.classify import Category
from commitminer.scoring import Candidate, MineResult

SCHEMA_VERSION = 1
"""Version of the per-candidate JSON object; bumped on incompatible changes."""


def candidate_to_json(candidate: Candidate, rank: int, repo: str) -> dict[str, Any]:
    """The JSON object written for one candidate."""
    commit, stats = candidate.commit, candidate.stats
    files: list[dict[str, Any]] = []
    for item in stats.files:
        record: dict[str, Any] = {
            "path": item.change.path,
            "category": item.category.value,
            "rule": item.classification.rule_id,
            "added": item.change.added,
            "deleted": item.change.deleted,
        }
        if item.change.old_path is not None:
            record["old_path"] = item.change.old_path
        files.append(record)
    return {
        "schema_version": SCHEMA_VERSION,
        "rank": rank,
        "repo": repo,
        "sha": commit.sha,
        "base": commit.base,
        "date": commit.date,
        "subject": commit.subject,
        "score": candidate.score,
        "features": [
            {
                "name": f.name,
                "value": f.value,
                "weight": f.weight,
                "contribution": f.contribution,
                "detail": f.detail,
            }
            for f in candidate.features
        ],
        "lines": {
            "changed": stats.changed_lines,
            "source_added": stats.added(Category.SOURCE),
            "source_deleted": stats.deleted(Category.SOURCE),
            "test_added": stats.added(Category.TEST),
            "test_deleted": stats.deleted(Category.TEST),
        },
        "source_files": [f.change.path for f in stats.source_files],
        "test_files": [f.change.path for f in stats.test_files],
        "files": files,
    }


def write_jsonl(path: Path, result: MineResult, repo: str) -> int:
    """Write every candidate, best first, one JSON object per line; return the count."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        json.dumps(candidate_to_json(c, rank, repo), sort_keys=True, ensure_ascii=True)
        for rank, c in enumerate(result.candidates, start=1)
    ]
    path.write_text("".join(line + "\n" for line in lines), encoding="ascii", newline="\n")
    return len(lines)


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _clip(text: str, width: int) -> str:
    text = _ascii(text)
    return text if len(text) <= width else text[: width - 3] + "..."


def render_summary(result: MineResult, repo: str) -> str:
    """One line: how many commits were walked, proposed and rejected (and why)."""
    reasons = ", ".join(f"{name} {count}" for name, count in result.rejected_by_reason().items())
    rejected = len(result.rejections)
    detail = f" ({reasons})" if reasons else ""
    return (
        f"{_ascii(repo)}: walked {result.walked} commits, "
        f"{len(result.candidates)} candidates, {rejected} rejected{detail}"
    )


def render_table(result: MineResult, top: int, subject_width: int = 56) -> str:
    """The ``top`` best candidates as a fixed-width table."""
    header = f"{'rank':>4}  {'score':>6}  {'sha':<10}  {'date':<10}  {'lines':>5}  "
    header += f"{'src':>3}  {'test':>4}  subject"
    rows = [header]
    for rank, c in enumerate(result.candidates[:top], start=1):
        rows.append(
            f"{rank:>4}  {c.score:>6.2f}  {c.commit.sha[:10]:<10}  {c.commit.date[:10]:<10}  "
            f"{c.stats.changed_lines:>5}  {len(c.stats.source_files):>3}  "
            f"{len(c.stats.test_files):>4}  {_clip(c.commit.subject, subject_width)}"
        )
    return "\n".join(rows)


def render_explanation(candidate: Candidate, rank: int) -> str:
    """The per-feature contribution table for one candidate."""
    rows = [
        f"#{rank} {candidate.commit.sha[:10]} score {candidate.score:.2f}: "
        f"{_clip(candidate.commit.subject, 60)}",
        f"  {'feature':<17} {'value':>6} {'weight':>6} {'contrib':>7}  detail",
    ]
    for f in candidate.features:
        rows.append(
            f"  {f.name:<17} {f.value:>6.3f} {f.weight:>6.2f} {f.contribution:>7.3f}"
            f"  {_clip(f.detail, 60)}"
        )
    return "\n".join(rows)
