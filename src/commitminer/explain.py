"""One commit, explained: its files, the filter verdict, and both contribution tables.

This is the output of ``commitminer explain SHA``. For a candidate it shows the
score and difficulty contributions and the difficulty band; for a rejected
commit, the reason code, what it means and, for ``oversize``, the limit that
was broken. The file table shows what the patch measurements saw per file.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from commitminer.export import (
    SCHEMA_VERSION,
    candidate_to_json,
    file_to_json,
    render_features,
)
from commitminer.filters import RejectReason, oversize_detail
from commitminer.models import Commit
from commitminer.scoring import Candidate, Rejection
from commitminer.settings import Settings
from commitminer.stats import ClassifiedFile, DiffStats


class ExplainError(LookupError):
    """The requested commit is not in the history, or the prefix is ambiguous."""


def find_commit(commits: Sequence[Commit], prefix: str) -> Commit:
    """The one commit whose sha starts with ``prefix`` (at least 4 hex digits)."""
    wanted = prefix.lower()
    if len(wanted) < 4 or any(char not in "0123456789abcdef" for char in wanted):
        raise ExplainError(f"{prefix!r} is not a commit sha (give at least 4 hex digits)")
    found = [c for c in commits if c.sha.startswith(wanted)]
    if not found:
        raise ExplainError(f"no commit {prefix} in the history (merge commits are not walked)")
    if len(found) > 1:
        raise ExplainError(f"{prefix} is ambiguous: {len(found)} commits start with it")
    return found[0]


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def _count(value: int | None) -> str:
    return "-" if value is None else str(value)


def _file_row(item: ClassifiedFile) -> str:
    change, patch = item.change, item.change.patch
    if patch is None:
        hunks = code = tests = asserts = "-" if change.binary else "?"
    else:
        hunks, code = str(patch.hunks), str(patch.code_hunks)
        tests, asserts = str(patch.test_lines), str(patch.asserts)
    path = _ascii(change.path)
    if change.old_path is not None:
        path = f"{_ascii(change.old_path)} -> {path}"
    return (
        f"    {item.category.value:<9} {item.classification.rule_id:<18} "
        f"{_count(change.added):>5} {_count(change.deleted):>5} {hunks:>5} {code:>5} "
        f"{tests:>5} {asserts:>7}  {path}"
    )


def render_files(stats: DiffStats) -> list[str]:
    """The file table: category, rule, line counts and patch measurements per file."""
    rows = [
        "  files",
        f"    {'category':<9} {'rule':<18} {'added':>5} {'del':>5} {'hunks':>5} {'code':>5} "
        f"{'tests':>5} {'asserts':>7}  path",
    ]
    rows += [_file_row(item) for item in stats.files]
    api = stats.public_api
    if api:
        rows.append(f"    public API touched: {', '.join(api)}")
    return rows


def _header(commit: Commit) -> list[str]:
    return [
        f"commit   {commit.sha}",
        f"base     {commit.base or '(root commit)'}",
        f"date     {commit.date}",
        f"subject  {_ascii(commit.subject)}",
    ]


def render_commit(outcome: Candidate | Rejection, settings: Settings) -> str:
    """The full explanation of one commit, as plain ASCII text."""
    rows = _header(outcome.commit)
    if isinstance(outcome, Rejection):
        rows.append(f"verdict  rejected ({outcome.reason.value}): {outcome.reason.description}")
        if outcome.reason is RejectReason.OVERSIZE:
            rows.append(f"         {oversize_detail(outcome.stats, settings)}")
        rows += ["", *render_files(outcome.stats)]
        return "\n".join(rows)
    level = outcome.difficulty
    rows.append(
        f"verdict  candidate: score {outcome.score:.2f} of 10, "
        f"difficulty {level.value:.2f} of 10 ({level.band})"
    )
    rows += ["", *render_files(outcome.stats), ""]
    rows += render_features("score (ranks candidates)", outcome.features, outcome.score)
    rows.append("")
    rows += render_features(f"difficulty ({settings.bands_label})", level.features, level.value)
    return "\n".join(rows)


def explain_json(outcome: Candidate | Rejection, settings: Settings, repo: str) -> dict[str, Any]:
    """The explanation as one JSON object (the candidate export plus the verdict)."""
    if isinstance(outcome, Candidate):
        record = candidate_to_json(outcome, None, repo)
        record["verdict"] = "candidate"
        return record
    commit = outcome.commit
    record = {
        "schema_version": SCHEMA_VERSION,
        "repo": repo,
        "sha": commit.sha,
        "base": commit.base,
        "date": commit.date,
        "subject": commit.subject,
        "verdict": "rejected",
        "reason": outcome.reason.value,
        "reason_description": outcome.reason.description,
        "files": [file_to_json(item) for item in outcome.stats.files],
    }
    if outcome.reason is RejectReason.OVERSIZE:
        record["reason_detail"] = oversize_detail(outcome.stats, settings)
    return record
