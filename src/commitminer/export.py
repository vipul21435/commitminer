"""Candidate export (JSON Lines) and plain-text rendering for the terminal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from commitminer.classify import Category
from commitminer.fingerprint import FINGERPRINT_VERSION, Fingerprint
from commitminer.models import PatchStats
from commitminer.scoring import Candidate, Feature, MineResult
from commitminer.stats import ClassifiedFile

SCHEMA_VERSION = 3
"""Version of the per-candidate JSON object; bumped on incompatible changes.

2: ``added_assertions`` score feature, ``difficulty``, per-file ``patch``, and
inline Rust test lines counted as test lines in ``lines``.
3: ``fingerprint`` (version, patch hash and sorted hunk hashes of the source
and test changes, or ``null``) and ``ledger`` (the dedupe verdict when mined
with ``--ledger``, else ``null``).
"""


def _feature_to_json(feature: Feature) -> dict[str, Any]:
    return {
        "name": feature.name,
        "value": feature.value,
        "weight": feature.weight,
        "contribution": feature.contribution,
        "detail": feature.detail,
    }


def patch_to_json(patch: PatchStats) -> dict[str, Any]:
    """Every patch measurement of one file (its hunk hashes are in the fingerprint)."""
    return {
        "hunks": patch.hunks,
        "code_hunks": patch.code_hunks,
        "code_added": patch.code_added,
        "code_deleted": patch.code_deleted,
        "test_added": patch.test_added,
        "test_deleted": patch.test_deleted,
        "asserts": patch.asserts,
        "api": list(patch.api),
    }


def file_to_json(item: ClassifiedFile) -> dict[str, Any]:
    """One changed file: path, category, rule, line counts, signals and patch measurements."""
    record: dict[str, Any] = {
        "path": item.change.path,
        "category": item.category.value,
        "rule": item.classification.rule_id,
        "added": item.change.added,
        "deleted": item.change.deleted,
    }
    if item.change.old_path is not None:
        record["old_path"] = item.change.old_path
    if item.change.signals:
        record["signals"] = list(item.change.signals)
    if item.change.patch is not None:
        record["patch"] = patch_to_json(item.change.patch)
    return record


def fingerprint_to_json(value: Fingerprint | None) -> dict[str, Any] | None:
    """The fingerprint object of the export, or ``None``."""
    if value is None:
        return None
    return {"version": FINGERPRINT_VERSION, "patch": value.patch, "hunks": list(value.hunks)}


def candidate_to_json(candidate: Candidate, rank: int | None, repo: str) -> dict[str, Any]:
    """The JSON object written for one candidate (``rank`` is ``None`` outside a ranking)."""
    commit, stats = candidate.commit, candidate.stats
    return {
        "schema_version": SCHEMA_VERSION,
        "rank": rank,
        "repo": repo,
        "sha": commit.sha,
        "base": commit.base,
        "date": commit.date,
        "subject": commit.subject,
        "score": candidate.score,
        "features": [_feature_to_json(f) for f in candidate.features],
        "difficulty": {
            "value": candidate.difficulty.value,
            "band": candidate.difficulty.band,
            "features": [_feature_to_json(f) for f in candidate.difficulty.features],
        },
        "public_api": list(stats.public_api or ()),
        "lines": {
            "changed": stats.changed_lines,
            "source_added": stats.added(Category.SOURCE),
            "source_deleted": stats.deleted(Category.SOURCE),
            "test_added": stats.added(Category.TEST),
            "test_deleted": stats.deleted(Category.TEST),
        },
        "source_files": [f.change.path for f in stats.source_files],
        "test_files": [f.change.path for f in stats.test_files],
        "inline_test_files": [f.change.path for f in stats.inline_test_files],
        "files": [file_to_json(item) for item in stats.files],
        "fingerprint": fingerprint_to_json(candidate.fingerprint),
        "ledger": None,
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


def _counts(counts: dict[str, int]) -> str:
    text = ", ".join(f"{name} {count}" for name, count in counts.items())
    return f" ({text})" if text else ""


def render_summary(result: MineResult, repo: str) -> str:
    """One line: commits walked, candidates (by band) and rejections (by reason)."""
    return (
        f"{_ascii(repo)}: walked {result.walked} commits, "
        f"{len(result.candidates)} candidates{_counts(result.bands())}, "
        f"{len(result.rejections)} rejected{_counts(result.rejected_by_reason())}"
    )


def render_table(result: MineResult, top: int, subject_width: int = 44) -> str:
    """The ``top`` best candidates as a fixed-width table.

    ``diff`` is the difficulty value and its band. The ``test`` column counts
    test files, plus ``+N`` source files that gained inline tests (Rust
    ``#[test]`` functions).
    """
    header = f"{'rank':>4}  {'score':>6}  {'diff':>11}  {'sha':<10}  {'date':<10}  "
    header += f"{'lines':>5}  {'src':>3}  {'test':>4}  subject"
    rows = [header]
    for rank, c in enumerate(result.candidates[:top], start=1):
        tests = str(len(c.stats.test_files))
        if c.stats.inline_test_files:
            # "0+1": no test file, one source file that gained inline tests.
            tests += f"+{len(c.stats.inline_test_files)}"
        band = f"{c.difficulty.value:.2f} {c.difficulty.band}"
        rows.append(
            f"{rank:>4}  {c.score:>6.2f}  {band:>11}  {c.commit.sha[:10]:<10}  "
            f"{c.commit.date[:10]:<10}  {c.stats.changed_lines:>5}  "
            f"{len(c.stats.source_files):>3}  {tests:>4}  {_clip(c.commit.subject, subject_width)}"
        )
    return "\n".join(rows)


def render_features(title: str, feats: tuple[Feature, ...], total: float) -> list[str]:
    """A contribution table: one row per feature, then the total."""
    rows = [
        f"  {title}",
        f"    {'feature':<17} {'value':>6} {'weight':>6} {'contrib':>7}  detail",
    ]
    for f in feats:
        rows.append(
            f"    {f.name:<17} {f.value:>6.3f} {f.weight:>6.2f} {f.contribution:>7.3f}"
            f"  {_clip(f.detail, 72)}"
        )
    weight = sum(f.weight for f in feats)
    rows.append(f"    {'total':<17} {'':>6} {weight:>6.2f} {total:>7.3f}")
    return rows


def render_explanation(candidate: Candidate, rank: int | None = None) -> str:
    """The score and difficulty contribution tables of one candidate."""
    where = f"#{rank} " if rank is not None else ""
    level = candidate.difficulty
    rows = [
        f"{where}{candidate.commit.sha[:10]} score {candidate.score:.2f}, "
        f"difficulty {level.value:.2f} ({level.band}): {_clip(candidate.commit.subject, 60)}",
        *render_features("score", candidate.features, candidate.score),
        *render_features("difficulty", level.features, level.value),
    ]
    return "\n".join(rows)
