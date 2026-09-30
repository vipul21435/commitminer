"""Candidate export (JSON Lines) and plain-text rendering for the terminal.

An export is one JSON object per line. The first line of a ``mine`` or
``prs`` export is the **run record** (``"kind": "run"``): what was mined, the
funnel counts, every rejected commit with its reason, the ledger summary and
the settings. Every other line is one **candidate** (``"kind": "candidate"``),
best first, self-contained: the repository URL, base and fix commits, the
classified files, the likely fail-to-pass test ids, both feature breakdowns,
the fingerprint and the ledger verdict. A ``batch`` export starts with a
**batch record** (``"kind": "batch"``, written by :mod:`commitminer.batch`)
and then holds one run record per repository, each followed by its
candidates. The committed JSON Schema (``schemas/export-v6.schema.json``,
printed by ``commitminer schema``) describes all three records; the tests
validate every export against it.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from commitminer import __version__
from commitminer.classify import Category
from commitminer.fingerprint import FINGERPRINT_VERSION, Fingerprint
from commitminer.ledger import Status, Verdict, ledger_verdict_to_json
from commitminer.models import Commit, PatchStats, PullRequest
from commitminer.scoring import Candidate, Feature, MineResult
from commitminer.settings import Settings
from commitminer.stats import ClassifiedFile

SCHEMA_VERSION = 6
"""Version of the export records; bumped on incompatible changes.

2: ``added_assertions`` score feature, ``difficulty``, per-file ``patch``, and
inline Rust test lines counted as test lines in ``lines``.
3: ``fingerprint`` (version, patch hash and sorted hunk hashes of the source
and test changes, or ``null``) and ``ledger`` (the dedupe verdict when mined
with ``--ledger``, else ``null``).
4: ``pull_request`` (number, URL, labels, linked issues, base, head and merge
commit shas, commits) for candidates mined with ``commitminer prs``, else
``null``.
5: ``kind`` on every record and a ``run`` record first; on candidates
``repo_url``, ``fail_to_pass`` (likely test ids from the test functions the
patch touched) and per-file ``patch.tests``; a committed JSON Schema.
6: ``batch`` exports (a batch record, then one run record per repository with
its candidates); ``resume`` on run records (``null`` outside a batch).
"""

SCHEMA_FILE = f"export-v{SCHEMA_VERSION}.schema.json"


def schema_text() -> str:
    """The JSON Schema of the export records, as committed in the package."""
    return resources.files("commitminer").joinpath("schemas", SCHEMA_FILE).read_text("utf-8")


@dataclass(frozen=True, slots=True)
class Resume:
    """How a batch run of one repository resumed from its watermark."""

    mode: str
    """``first``, ``resumed``, ``up-to-date`` or ``full``."""
    previous: str | None
    """The watermark before the run."""
    watermark: str | None
    """The watermark after the run."""
    skipped: int = 0
    """Commits or pull requests passed over because a batch had evaluated them."""
    note: str | None = None


@dataclass(frozen=True, slots=True)
class Run:
    """What one ``mine``, ``prs`` or batch run looked at: the run record's identity."""

    repo: str
    url: str | None
    source: str
    """``clone``, ``history`` or ``pull-requests``."""
    unit: str
    """``commits`` or ``pull requests``."""
    settings: Settings
    ledger: str | None = None
    min_overlap: float | None = None
    new_only: bool = False
    resume: Resume | None = None


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
        "tests": list(patch.tests),
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


def pull_request_to_json(pull: PullRequest | None) -> dict[str, Any] | None:
    """The ``pull_request`` object of the export, or ``None`` for a plain commit."""
    if pull is None:
        return None
    return {
        "number": pull.number,
        "url": pull.url,
        "title": pull.title,
        "merged_at": pull.merged_at,
        "base_ref": pull.base_ref,
        "base_sha": pull.base_sha,
        "head_sha": pull.head_sha,
        "merge_commit_sha": pull.merge_commit_sha,
        "labels": list(pull.labels),
        "linked_issues": list(pull.linked_issues),
        "commits": list(pull.commits),
    }


def fingerprint_to_json(value: Fingerprint | None) -> dict[str, Any] | None:
    """The fingerprint object of the export, or ``None``."""
    if value is None:
        return None
    return {"version": FINGERPRINT_VERSION, "patch": value.patch, "hunks": list(value.hunks)}


def candidate_to_json(
    candidate: Candidate,
    rank: int | None,
    repo: str,
    verdict: Verdict | None = None,
    url: str | None = None,
) -> dict[str, Any]:
    """The JSON object written for one candidate (``rank`` is ``None`` outside a ranking).

    ``verdict`` is the ledger's verdict when the candidates were checked
    against one; ``url`` is the repository URL, when known.
    """
    commit, stats = candidate.commit, candidate.stats
    return {
        "kind": "candidate",
        "schema_version": SCHEMA_VERSION,
        "rank": rank,
        "repo": repo,
        "repo_url": url,
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
        "fail_to_pass": list(stats.fail_to_pass),
        "files": [file_to_json(item) for item in stats.files],
        "fingerprint": fingerprint_to_json(candidate.fingerprint),
        "ledger": None if verdict is None else ledger_verdict_to_json(verdict),
        "pull_request": pull_request_to_json(commit.pull_request),
    }


def _verdicts(result: MineResult, verdicts: Sequence[Verdict] | None) -> list[Verdict | None]:
    if verdicts is None:
        return [None] * len(result.candidates)
    if len(verdicts) != len(result.candidates):
        raise ValueError("one ledger verdict per candidate is needed")
    return list(verdicts)


def _ranks(result: MineResult, ranks: Sequence[int] | None) -> Sequence[int]:
    return range(1, len(result.candidates) + 1) if ranks is None else ranks


def settings_to_json(settings: Settings) -> dict[str, Any]:
    """The limits, caps, weights and bands a run used (the classifier rules are left out)."""
    record = asdict(settings)
    del record["rules"]
    return record


def run_to_json(
    run: Run,
    result: MineResult,
    all_verdicts: Sequence[Verdict] | None,
    exported: int,
) -> dict[str, Any]:
    """The run record: the funnel, every rejected commit with its reason, ledger and settings.

    ``all_verdicts`` are the ledger verdicts of every candidate of ``result``
    (before ``--new-only`` narrowed the export), or ``None`` without a ledger;
    ``exported`` is how many candidate records follow.
    """
    ledger: dict[str, Any] | None = None
    if run.ledger is not None:
        counts = Counter(v.status for v in all_verdicts or ())
        ledger = {
            "path": run.ledger,
            "min_overlap": run.min_overlap,
            "new_only": run.new_only,
            "counts": {status.value: counts[status] for status in Status},
        }
    return {
        "kind": "run",
        "schema_version": SCHEMA_VERSION,
        "commitminer": __version__,
        "repo": run.repo,
        "url": run.url,
        "source": run.source,
        "unit": run.unit,
        "walked": result.walked,
        "candidates": len(result.candidates),
        "bands": result.bands(),
        "rejected": result.rejected_by_reason(),
        "rejections": [
            {
                "sha": r.commit.sha,
                "date": r.commit.date,
                "subject": r.commit.subject,
                "reason": r.reason.value,
                "pull_request": None
                if r.commit.pull_request is None
                else r.commit.pull_request.number,
            }
            for r in result.rejections
        ],
        "ledger": ledger,
        "exported": exported,
        "settings": settings_to_json(run.settings),
        "resume": None if run.resume is None else asdict(run.resume),
    }


def export_records(
    result: MineResult,
    repo: str,
    verdicts: Sequence[Verdict] | None = None,
    ranks: Sequence[int] | None = None,
    run: Run | None = None,
    funnel: MineResult | None = None,
    all_verdicts: Sequence[Verdict] | None = None,
) -> list[dict[str, Any]]:
    """The run record (with ``run``), then every candidate record, best first.

    ``ranks`` are the candidates' ranks when ``result`` holds only some of
    them (``--new-only``); ``funnel`` and ``all_verdicts`` are then the full
    result and its verdicts, which the run record describes.
    """
    rows = [
        candidate_to_json(c, rank, repo, verdict, run.url if run else None)
        for rank, c, verdict in zip(
            _ranks(result, ranks), result.candidates, _verdicts(result, verdicts), strict=True
        )
    ]
    if run is None:
        return rows
    return [run_to_json(run, funnel or result, all_verdicts, len(rows)), *rows]


def write_records(path: Path, records: Sequence[dict[str, Any]]) -> None:
    """Write records as JSON Lines: sorted keys, ASCII only, one object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(record, sort_keys=True, ensure_ascii=True) for record in records]
    path.write_text("".join(line + "\n" for line in lines), encoding="ascii", newline="\n")


def write_jsonl(
    path: Path,
    result: MineResult,
    repo: str,
    verdicts: Sequence[Verdict] | None = None,
    ranks: Sequence[int] | None = None,
    run: Run | None = None,
    funnel: MineResult | None = None,
    all_verdicts: Sequence[Verdict] | None = None,
) -> int:
    """Write the export of one run (see :func:`export_records`); returns the candidates written."""
    records = export_records(result, repo, verdicts, ranks, run, funnel, all_verdicts)
    write_records(path, records)
    return sum(1 for record in records if record["kind"] == "candidate")


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def reference(commit: Commit) -> str:
    """How tables name a commit: ``#123`` for a pull request, else the first 10 sha digits."""
    if commit.pull_request is not None:
        return f"#{commit.pull_request.number}"
    return commit.sha[:10]


def _clip(text: str, width: int) -> str:
    text = _ascii(text)
    return text if len(text) <= width else text[: width - 3] + "..."


def _counts(counts: dict[str, int]) -> str:
    text = ", ".join(f"{name} {count}" for name, count in counts.items())
    return f" ({text})" if text else ""


def render_summary(result: MineResult, repo: str, unit: str = "commits") -> str:
    """One line: commits (or pull requests) walked, candidates by band, rejections by reason."""
    return (
        f"{_ascii(repo)}: walked {result.walked} {unit}, "
        f"{len(result.candidates)} candidates{_counts(result.bands())}, "
        f"{len(result.rejections)} rejected{_counts(result.rejected_by_reason())}"
    )


_LEDGER_LABELS = {
    Status.NEW: "new",
    Status.DUPLICATE: "dup",
    Status.OVERLAP: "overlap",
    Status.UNKNOWN: "?",
}


def render_table(
    result: MineResult,
    top: int,
    subject_width: int = 44,
    verdicts: Sequence[Verdict] | None = None,
    ranks: Sequence[int] | None = None,
) -> str:
    """The ``top`` best candidates as a fixed-width table.

    ``diff`` is the difficulty value and its band. The ``test`` column counts
    test files, plus ``+N`` source files that gained inline tests (Rust
    ``#[test]`` functions). With ``verdicts``, a ``ledger`` column shows each
    candidate's ledger status.
    """
    marks = _verdicts(result, verdicts)
    pulls = any(c.commit.pull_request is not None for c in result.candidates)
    header = f"{'rank':>4}  {'score':>6}  {'diff':>11}  {'pull' if pulls else 'sha':<10}  "
    header += f"{'date':<10}  "
    header += f"{'lines':>5}  {'src':>3}  {'test':>4}  "
    if verdicts is not None:
        header += f"{'ledger':<7}  "
    rows = [header + "subject"]
    rows_in = zip(_ranks(result, ranks), result.candidates, marks, strict=True)
    for index, (rank, c, verdict) in enumerate(rows_in):
        if index == top:
            break
        tests = str(len(c.stats.test_files))
        if c.stats.inline_test_files:
            # "0+1": no test file, one source file that gained inline tests.
            tests += f"+{len(c.stats.inline_test_files)}"
        band = f"{c.difficulty.value:.2f} {c.difficulty.band}"
        ledger = "" if verdict is None else f"{_LEDGER_LABELS[verdict.status]:<7}  "
        rows.append(
            f"{rank:>4}  {c.score:>6.2f}  {band:>11}  {reference(c.commit):<10}  "
            f"{c.commit.date[:10]:<10}  {c.stats.changed_lines:>5}  "
            f"{len(c.stats.source_files):>3}  {tests:>4}  {ledger}"
            f"{_clip(c.commit.subject, subject_width)}"
        )
    return "\n".join(rows)


def render_ledger(
    result: MineResult,
    verdicts: Sequence[Verdict],
    where: str,
    ranks: Sequence[int] | None = None,
    repo: str | None = None,
) -> str:
    """The ledger summary line, then one line per candidate that is not new.

    ``ranks`` are the candidates' ranks when ``result`` holds only some of them;
    ``repo`` is the candidates' repository label.
    """
    counts = Counter(verdict.status for verdict in _verdicts(result, verdicts) if verdict)
    summary = ", ".join(f"{counts[status]} {status.value}" for status in Status if counts[status])
    rows = [f"ledger {_ascii(where)}: {summary or 'no candidates'}"]
    for rank, candidate, verdict in zip(
        _ranks(result, ranks), result.candidates, verdicts, strict=True
    ):
        if verdict.status is not Status.NEW:
            rows.append(
                f"  #{rank} {reference(candidate.commit)} "
                f"{_ascii(verdict.describe(candidate.commit.sha, repo))}"
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
        f"{where}{reference(candidate.commit)} score {candidate.score:.2f}, "
        f"difficulty {level.value:.2f} ({level.band}): {_clip(candidate.commit.subject, 60)}",
        *render_features("score", candidate.features, candidate.score),
        *render_features("difficulty", level.features, level.value),
    ]
    return "\n".join(rows)
