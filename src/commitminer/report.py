"""Markdown and HTML reports from an export file (``commitminer report``).

The report is built once as a small document (headings, paragraphs, tables,
collapsible sections) from the run record and the candidate records of a
``mine --out`` or ``prs --out`` file, then rendered either as Markdown or as
one self-contained HTML page: no scripts, no external stylesheets, fonts or
images, and every string from the export escaped (HTML entities in HTML,
backslashes before Markdown punctuation in Markdown). Links point at the
repository's commits or pull requests only when the export's URL is an http(s)
URL; anything else is printed as text.

Sections: the funnel (walked, rejected per reason, candidates, ledger verdicts,
exported), the ranked table with one column per score feature contribution,
one section per candidate (files, likely fail-to-pass tests, fingerprint,
ledger verdict), the rejected commits grouped by reason, and the settings.

A ``batch --out`` file (a batch record, then one run record per repository
with its candidates) gives one report for the whole batch: every repository
with how it resumed and what the ledger said about its candidates, the
collisions across repositories, the best new candidates of the batch, and
then each repository's sections as above, one heading level down.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from commitminer.export import SCHEMA_VERSION
from commitminer.filters import RejectReason
from commitminer.sanitize import strip_credentials, without_surrogates

FORMATS: Final = ("markdown", "html")
_SUFFIXES: Final = {".md": "markdown", ".markdown": "markdown", ".html": "html", ".htm": "html"}
_HTTP: Final = re.compile(r"^https?://", re.IGNORECASE)
_SSH_REMOTE: Final = re.compile(
    r"^(?:ssh://(?:[\w.-]+@)?(?P<host>[\w.-]+)/|(?:[\w.-]+@)(?P<scp_host>[\w.-]+):)(?P<path>.+)$"
)
"""``ssh://[user@]host/path`` or ``user@host:path`` (the user is required in the scp form)."""
_MD_SPECIAL: Final = re.compile(r"([\\`*_\[\]<>|~])")
"""Punctuation that would format or break a table cell; escaped with a backslash."""


class ReportError(ValueError):
    """The export file cannot be read as a schema version 5 or 6 export."""


READABLE: Final = (5, SCHEMA_VERSION)
"""Export schema versions the report reads: 6 only adds batch exports and ``resume``."""


# --- reading the export ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Export:
    """The run record and the candidate records of one export file (or of one batch run)."""

    run: dict[str, Any]
    candidates: list[dict[str, Any]]


@dataclass(frozen=True, slots=True)
class BatchExport:
    """The batch record and every repository's run of a ``batch --out`` file."""

    batch: dict[str, Any]
    runs: list[Export]


Kind = type | tuple[type, ...]


def _is(value: Any, kind: Kind) -> bool:
    """``isinstance``, except that a bool is not a number."""
    return isinstance(value, kind) and (kind is bool or not isinstance(value, bool))


def _wrong(where: str, kind: Kind, value: Any, null: bool = False) -> ReportError:
    expected = kind.__name__ if isinstance(kind, type) else " or ".join(k.__name__ for k in kind)
    expected += " or null" if null else ""
    return ReportError(f"{where}: expected {expected}, got {type(value).__name__}")


def _field(record: dict[str, Any], key: str, kind: Kind, where: str) -> Any:
    if key not in record:
        raise ReportError(f"{where}: missing field {key!r}")
    value = record[key]
    if not _is(value, kind):
        raise _wrong(f"{where}.{key}", kind, value)
    return value


def _values(mapping: dict[str, Any], kind: Kind, where: str) -> None:
    """Every value of ``mapping`` is a ``kind``: the report computes with them."""
    for name, value in mapping.items():
        if not _is(value, kind):
            raise _wrong(f"{where}.{name}", kind, value)


def _optional_object(record: dict[str, Any], key: str, where: str) -> dict[str, Any] | None:
    value = record.get(key)
    if value is not None and not isinstance(value, dict):
        raise _wrong(f"{where}.{key}", dict, value, null=True)
    return value


def _check_run(record: dict[str, Any], where: str) -> None:
    """The run record's fields, and the nested values the funnel and settings compute with."""
    for key, expected in _RUN_FIELDS.items():
        _field(record, key, expected, where)
    _values(record["rejected"], int, f"{where}.rejected")
    _values(record["bands"], int, f"{where}.bands")
    ledger = _optional_object(record, "ledger", where)
    if ledger is not None:
        counts = ledger.get("counts", {})
        if not isinstance(counts, dict):
            raise _wrong(f"{where}.ledger.counts", dict, counts)
        _values(counts, int, f"{where}.ledger.counts")
    for key, value in record["settings"].items():
        if isinstance(value, dict):
            _values(value, (int, float), f"{where}.settings.{key}")


def _check_candidate(record: dict[str, Any], where: str) -> None:
    for key, expected in _CANDIDATE_FIELDS.items():
        _field(record, key, expected, where)
    _optional_object(record, "pull_request", where)


_RUN_FIELDS: Final[dict[str, type | tuple[type, ...]]] = {
    "repo": str,
    "unit": str,
    "source": str,
    "commitminer": str,
    "walked": int,
    "candidates": int,
    "exported": int,
    "bands": dict,
    "rejected": dict,
    "rejections": list,
    "settings": dict,
}
_BATCH_FIELDS: Final[dict[str, type | tuple[type, ...]]] = {
    "config": str,
    "ledger": str,
    "commitminer": str,
    "min_overlap": (int, float),
    "dry_run": bool,
    "full": bool,
    "runs": int,
    "failed": list,
    "collisions": list,
}
_CANDIDATE_FIELDS: Final[dict[str, type | tuple[type, ...]]] = {
    "rank": int,
    "repo": str,
    "sha": str,
    "date": str,
    "subject": str,
    "score": (int, float),
    "difficulty": dict,
    "features": list,
    "lines": dict,
    "source_files": list,
    "test_files": list,
    "fail_to_pass": list,
}


def _record(line: str, number: int) -> dict[str, Any]:
    where = f"line {number}"
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ReportError(f"{where}: invalid JSON: {exc.msg}") from exc
    return _checked(record, number)


def _checked(record: Any, number: int) -> dict[str, Any]:
    where = f"line {number}"
    if not isinstance(record, dict):
        raise ReportError(f"{where}: expected an object")
    if record.get("schema_version") not in READABLE:
        raise ReportError(
            f"{where}: schema_version {record.get('schema_version')!r}, expected "
            f"{' or '.join(map(str, READABLE))} (mine the candidates again)"
        )
    return record


def _runs(records: Sequence[tuple[int, dict[str, Any]]], where: str, batch: bool) -> list[Export]:
    """Split records into runs: each run record followed by its candidates."""
    runs: list[Export] = []
    for number, record in records:
        kind = record.get("kind")
        if kind == "run" and (batch or not runs):
            _check_run(record, f"line {number}")
            runs.append(Export(record, []))
        elif kind == "candidate" and runs:
            _check_candidate(record, f"line {number}")
            runs[-1].candidates.append(record)
        elif batch and not runs:
            raise ReportError(f"{where}: line {number}: expected a run record after the batch")
        else:
            raise ReportError(f"{where}: line {number}: expected a candidate record")
    for run in runs:
        if len(run.candidates) != run.run["exported"]:
            raise ReportError(
                f"{where}: the run record of {run.run['repo']} says {run.run['exported']} "
                f"candidates, the file has {len(run.candidates)}"
            )
    return runs


def assemble(records: Sequence[tuple[int, dict[str, Any]]], where: str) -> Export | BatchExport:
    """Checked records, numbered by line, as one run's export or a batch export."""
    if not records:
        raise ReportError(f"{where}: empty file")
    number, first = records[0]
    if first.get("kind") == "batch":
        for key, kind in _BATCH_FIELDS.items():
            _field(first, key, kind, f"line {number}")
        runs = _runs(records[1:], where, batch=True)
        if len(runs) != first["runs"]:
            raise ReportError(
                f"{where}: the batch record says {first['runs']} runs, the file has {len(runs)}"
            )
        return BatchExport(first, runs)
    if first.get("kind") != "run":
        raise ReportError(
            f"{where}: line {number}: the first record must be the run record "
            "(or the batch record of a batch export)"
        )
    (run,) = _runs(records, where, batch=False)
    return run


def from_records(records: Sequence[dict[str, Any]], where: str) -> Export | BatchExport:
    """An export held in memory (as ``batch --report`` has it), checked like a file."""
    return assemble([(n, _checked(r, n)) for n, r in enumerate(records, start=1)], where)


def read_export(path: Path) -> Export | BatchExport:
    """Read an export: a run record and its candidates, or a batch record and its runs."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ReportError(f"{path}: cannot read: {exc}") from exc
    lines = [(n, line) for n, line in enumerate(text.splitlines(), start=1) if line.strip()]
    return assemble([(n, _record(line, n)) for n, line in lines], str(path))


# --- the document ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Link:
    """A link, rendered as text when the URL is not http(s)."""

    text: str
    url: str | None


Cell = str | Link


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str


@dataclass(frozen=True, slots=True)
class Paragraph:
    parts: tuple[Cell, ...]


@dataclass(frozen=True, slots=True)
class Items:
    items: tuple[tuple[Cell, ...], ...]


@dataclass(frozen=True, slots=True)
class Table:
    headers: tuple[str, ...]
    rows: tuple[tuple[Cell, ...], ...]
    numeric: frozenset[int] = frozenset()
    """Indexes of right-aligned columns."""


@dataclass(frozen=True, slots=True)
class Details:
    """A collapsible section in HTML; a heading and its body in Markdown."""

    level: int
    summary: str
    body: tuple[Block, ...]


Block = Heading | Paragraph | Items | Table | Details


def repo_web_url(url: str | None) -> str | None:
    """An http(s) URL for the repository, from a web URL or a git remote, else ``None``."""
    if not url:
        return None
    url = strip_credentials(url.strip()).removesuffix("/").removesuffix(".git")
    if _HTTP.match(url):
        return url
    remote = _SSH_REMOTE.match(url)
    if remote is not None:
        host = remote.group("host") or remote.group("scp_host")
        return f"https://{host}/{remote.group('path').lstrip('/')}"
    return None


def _commit_link(sha: str, base: str | None, pull: dict[str, Any] | None) -> Link:
    """``#123`` linking to the pull request, or the short sha linking to the commit."""
    if isinstance(pull, dict) and isinstance(pull.get("number"), int):
        url = pull.get("url")
        if not (isinstance(url, str) and _HTTP.match(url)):
            url = f"{base}/pull/{pull['number']}" if base else None
        return Link(f"#{pull['number']}", url)
    return Link(sha[:10], f"{base}/commit/{sha}" if base else None)


def _funnel(run: dict[str, Any]) -> Table:
    unit = run["unit"]
    remaining = run["walked"]
    rows: list[tuple[Cell, ...]] = [(f"walked {unit}", str(remaining), str(remaining))]
    rejected = run["rejected"]
    for reason in RejectReason:
        count = rejected.get(reason.value)
        if not count:
            continue
        remaining -= count
        rows.append(
            (f"rejected: {reason.value} ({reason.description})", str(count), str(remaining))
        )
    remaining = run["candidates"]
    rows.append(("candidates", str(remaining), str(remaining)))
    ledger = run.get("ledger")
    if isinstance(ledger, dict):
        counts = ledger.get("counts", {})
        for status in ("duplicate", "overlap", "unknown"):
            count = counts.get(status, 0)
            if count:
                remaining -= count
                rows.append((f"ledger: {status}", str(count), str(remaining)))
        rows.append(("ledger: new", str(counts.get("new", 0)), str(remaining)))
    rows.append(("exported", str(run["exported"]), str(run["exported"])))
    return Table(("step", "count", "remaining"), tuple(rows), frozenset({1, 2}))


def _bands(run: dict[str, Any]) -> str:
    bands = run["bands"]
    text = ", ".join(
        f"{band} {bands[band]}" for band in ("easy", "medium", "hard") if bands.get(band)
    )
    return f"Difficulty bands: {text}." if text else "No candidates."


def _feature_names(candidates: Sequence[dict[str, Any]]) -> tuple[str, ...]:
    names: list[str] = []
    for candidate in candidates:
        for feature in candidate["features"]:
            name = feature.get("name") if isinstance(feature, dict) else None
            if isinstance(name, str) and name not in names:
                names.append(name)
    return tuple(names)


def _num(value: Any, digits: int) -> str:
    """A number with ``digits`` decimals, or ``?`` for anything that is not one."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return "?"
    return f"{value:.{digits}f}"


def _contribution(candidate: dict[str, Any], name: str) -> str:
    for feature in candidate["features"]:
        if isinstance(feature, dict) and feature.get("name") == name:
            return _num(feature.get("contribution"), 2)
    return ""


def _ledger_status(candidate: dict[str, Any]) -> str:
    verdict = candidate.get("ledger")
    if not isinstance(verdict, dict):
        return ""
    status = verdict.get("status")
    return status if isinstance(status, str) else "?"


def _ranked(candidates: Sequence[dict[str, Any]], base: str | None, ledger: bool) -> Table:
    names = _feature_names(candidates)
    headers = ["rank", "commit", "date", "score", "difficulty", *names, "lines", "src", "test"]
    headers += ["f2p"]
    if ledger:
        headers.append("ledger")
    headers.append("subject")
    rows: list[tuple[Cell, ...]] = []
    for candidate in candidates:
        difficulty = candidate["difficulty"]
        level = f"{_num(difficulty.get('value'), 2)} {difficulty.get('band', '?')}"
        row: list[Cell] = [
            str(candidate["rank"]),
            _commit_link(candidate["sha"], base, candidate.get("pull_request")),
            str(candidate["date"])[:10],
            _num(candidate["score"], 2),
            level,
            *(_contribution(candidate, name) for name in names),
            str(candidate["lines"].get("changed", "?")),
            str(len(candidate["source_files"])),
            str(len(candidate["test_files"])),
            str(len(candidate["fail_to_pass"])),
        ]
        if ledger:
            row.append(_ledger_status(candidate))
        row.append(str(candidate["subject"]))
        rows.append(tuple(row))
    text_columns = ("commit", "date", "ledger", "subject")
    numeric = frozenset(i for i, header in enumerate(headers) if header not in text_columns)
    return Table(tuple(headers), tuple(rows), numeric)


def _paths(paths: Iterable[Any]) -> str:
    return ", ".join(str(p) for p in paths) or "none"


def _verdict_text(candidate: dict[str, Any]) -> str:
    verdict = candidate.get("ledger")
    if not isinstance(verdict, dict):
        return "not checked"
    status = str(verdict.get("status", "?"))
    matches = verdict.get("matches")
    if not isinstance(matches, list) or not matches or not isinstance(matches[0], dict):
        return status
    best = matches[0]
    where = f"{best.get('repo', '?')} {str(best.get('sha', '?'))[:10]}"
    same = "same commit as" if best.get("sha") == candidate["sha"] else "same fix as"
    if best.get("source") == "run":
        return f"{status}: {same} {where} (earlier in this run)"
    owner = f" by {best['owner']}" if best.get("owner") else ""
    when = str(best.get("first_seen") or "")[:10]
    detail = f"{best.get('status', '?')}{owner} on {when}"
    if best.get("exact") and same == "same commit as" and best.get("repo") == candidate["repo"]:
        return f"{status}: already in the ledger ({detail})"
    if best.get("exact"):
        return f"{status}: {same} {where} ({detail})"
    shared = f"{best.get('shared', '?')} of {best.get('hunks', '?')} hunks shared"
    return f"{status}: {shared} with {where} ({detail})"


def _candidate_section(candidate: dict[str, Any], base: str | None, level: int = 3) -> Details:
    sha = str(candidate["sha"])
    pull = candidate.get("pull_request")
    link = _commit_link(sha, base, pull)
    items: list[tuple[Cell, ...]] = [
        ("commit: ", Link(sha, f"{base}/commit/{sha}" if base else None)),
        ("base: ", str(candidate.get("base") or "(root commit)")),
    ]
    if isinstance(pull, dict):
        url = str(pull.get("url", ""))
        items.append((f"pull request #{pull.get('number')}: ", Link(url, url)))
    items += [
        ("source files: " + _paths(candidate["source_files"]),),
        ("test files: " + _paths(candidate["test_files"]),),
        ("likely fail-to-pass tests: " + _paths(candidate["fail_to_pass"]),),
    ]
    fingerprint = candidate.get("fingerprint")
    if isinstance(fingerprint, dict):
        hunks = fingerprint.get("hunks")
        count = len(hunks) if isinstance(hunks, list) else "?"
        items.append(
            (f"fingerprint: {fingerprint.get('patch', '?')} ({count} source and test hunks)",)
        )
    else:
        items.append(("fingerprint: none",))
    items.append(("ledger: " + _verdict_text(candidate),))
    features = candidate["features"]
    rows = tuple(
        (
            str(f.get("name", "?")),
            _num(f.get("value"), 3),
            _num(f.get("weight"), 2),
            _num(f.get("contribution"), 3),
            str(f.get("detail", "")),
        )
        for f in features
        if isinstance(f, dict)
    )
    summary = f"#{candidate['rank']} {link.text}: {candidate['subject']}"
    return Details(
        level,
        summary,
        (
            Items(tuple(items)),
            Table(("feature", "value", "weight", "contrib", "detail"), rows, frozenset({1, 2, 3})),
        ),
    )


def _rejected(run: dict[str, Any], base: str | None, level: int = 3) -> list[Block]:
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for rejection in run["rejections"]:
        if isinstance(rejection, dict):
            by_reason.setdefault(str(rejection.get("reason", "?")), []).append(rejection)
    blocks: list[Block] = []
    known = [reason.value for reason in RejectReason]
    for reason in sorted(
        by_reason, key=lambda r: (known.index(r) if r in known else len(known), r)
    ):
        entries = by_reason[reason]
        description = RejectReason(reason).description if reason in known else ""
        rows = tuple(
            (
                _commit_link(
                    str(e.get("sha", "")),
                    base,
                    {"number": e["pull_request"]} if e.get("pull_request") else None,
                ),
                str(e.get("date", ""))[:10],
                str(e.get("subject", "")),
            )
            for e in entries
        )
        summary = f"{reason} ({len(entries)})" + (f": {description}" if description else "")
        blocks.append(Details(level, summary, (Table(("commit", "date", "subject"), rows),)))
    if not blocks:
        blocks.append(Paragraph(("No commit was rejected.",)))
    return blocks


def _settings(run: dict[str, Any]) -> Table:
    settings = run["settings"]
    rows: list[tuple[Cell, ...]] = []
    for key, value in settings.items():
        if isinstance(value, dict):
            text = ", ".join(f"{name} {weight:g}" for name, weight in value.items())
            rows.append((str(key), text))
        else:
            rows.append((str(key), f"{value:g}" if isinstance(value, int | float) else str(value)))
    return Table(("setting", "value"), tuple(rows))


_SOURCES: Final = {
    "clone": "a local clone",
    "history": "a recorded history",
    "pull-requests": "the merged pull requests",
}


def _base(run: dict[str, Any]) -> str | None:
    return repo_web_url(run.get("url") if isinstance(run.get("url"), str) else None)


def _run_blocks(export: Export, top: int | None, level: int) -> list[Block]:
    """The sections of one run, with headings at ``level`` (collapsible parts one below)."""
    run = export.run
    base = _base(run)
    shown = export.candidates if top is None else export.candidates[:top]
    ledger = isinstance(run.get("ledger"), dict)
    intro: list[Cell] = [f"Source: {_SOURCES.get(run['source'], run['source'])}"]
    url = run.get("url")
    if isinstance(url, str) and url:
        intro += [" of ", Link(url, base)]
    intro.append(
        f", {run['walked']} {run['unit']} walked. CommitMiner {run['commitminer']}, "
        f"export schema version {run['schema_version']}."
    )
    ledger_line: list[Block] = []
    if ledger:
        info = run["ledger"]
        narrowed = (
            ", duplicates and overlaps left out of the export" if info.get("new_only") else ""
        )
        ledger_line.append(
            Paragraph(
                (
                    f"Checked against the ledger {info.get('path', '?')} "
                    f"(min overlap {info.get('min_overlap', '?')}{narrowed}).",
                )
            )
        )
    ranked = _ranked(shown, base, ledger)
    shown_text = (
        f"All {len(shown)} exported candidates."
        if top is None or len(export.candidates) <= top
        else f"The best {len(shown)} of {len(export.candidates)} exported candidates."
    )
    return [
        Paragraph(tuple(intro)),
        *ledger_line,
        Heading(level, "Funnel"),
        _funnel(run),
        Paragraph((_bands(run),)),
        Heading(level, "Ranked candidates"),
        Paragraph(
            (
                f"{shown_text} Score columns are each feature's contribution "
                "(weight times value); f2p counts the likely fail-to-pass tests.",
            )
        ),
        ranked,
        Heading(level, "Candidates"),
        *(_candidate_section(c, base, level + 1) for c in shown),
        Heading(level, "Rejected commits"),
        *_rejected(run, base, level + 1),
        Heading(level, "Settings"),
        _settings(run),
    ]


def build(export: Export | BatchExport, top: int | None = None) -> list[Block]:
    """The report as blocks; ``top`` limits the ranked table and the candidate sections."""
    if isinstance(export, BatchExport):
        return build_batch(export, top)
    return [Heading(1, f"CommitMiner report: {export.run['repo']}"), *_run_blocks(export, top, 2)]


def outcome_of(candidate: dict[str, Any]) -> str:
    """What the ledger said about an exported candidate, as :func:`commitminer.batch.outcome`."""
    verdict = candidate.get("ledger")
    status = verdict.get("status") if isinstance(verdict, dict) else None
    matches = verdict.get("matches") if isinstance(verdict, dict) else None
    if status not in ("duplicate", "overlap") or not isinstance(matches, list) or not matches:
        return str(status or "unknown")
    others = [m for m in matches if isinstance(m, dict)]
    best = others[0] if others else {}
    if best.get("repo") == candidate["repo"] and best.get("sha") == candidate["sha"]:
        return "recorded"
    if any(m.get("repo") != candidate["repo"] for m in others):
        return "collision"
    return "internal"


_RESUMED: Final = {
    "first": "first run",
    "resumed": "resumed from {previous}",
    "up-to-date": "up to date",
    "full": "full walk",
}


def _stamp(date: Any) -> float:
    """Epoch seconds of an ISO 8601 date, for newest-first ties; 0 if it is not one."""
    try:
        return datetime.fromisoformat(str(date)).timestamp()
    except ValueError:
        return 0.0


def _short(position: Any) -> str:
    if not isinstance(position, str) or not position:
        return "-"
    return position[:10] if re.fullmatch(r"[0-9a-f]{40,64}", position) else position


def _repositories(batch: BatchExport) -> Table:
    rows: list[tuple[Cell, ...]] = []
    for export in batch.runs:
        run = export.run
        resume = run.get("resume")
        if not isinstance(resume, dict):
            resume = {}
        mode = str(resume.get("mode", "-"))
        how = _RESUMED.get(mode, mode).format(previous=_short(resume.get("previous")))
        if resume.get("note"):
            how += f" ({resume['note']})"
        counts = [outcome_of(c) for c in export.candidates]
        rows.append(
            (
                Link(str(run["repo"]), _base(run)),
                str(run["source"]),
                how,
                str(run["walked"]),
                str(resume.get("skipped", 0)),
                str(run["candidates"]),
                *(str(counts.count(k)) for k in ("new", "recorded", "internal", "collision")),
                _short(resume.get("watermark")),
            )
        )
    for failed in batch.batch["failed"]:
        if isinstance(failed, dict):
            error = f"failed: {failed.get('error', '?')}"
            rows.append(
                (str(failed.get("repo", "?")), str(failed.get("source", "?")), error) + ("",) * 8
            )
    headers = (
        "repository",
        "source",
        "run",
        "walked",
        "evaluated before",
        "candidates",
        "new",
        "already recorded",
        "same repository",
        "collisions",
        "watermark",
    )
    return Table(headers, tuple(rows), frozenset(range(3, 10)))


def _collisions(batch: BatchExport, bases: dict[str, str | None]) -> list[Block]:
    collisions = [c for c in batch.batch["collisions"] if isinstance(c, dict)]
    if not collisions:
        return [Paragraph(("No candidate collides with a fix recorded under another repository.",))]
    same = sum(1 for c in collisions if c.get("same_commit"))
    rows: list[tuple[Cell, ...]] = []
    for c in collisions:
        match = c.get("match")
        if not isinstance(match, dict):
            match = {}
        repo, other = str(c.get("repo", "?")), str(match.get("repo", "?"))
        sha, other_sha = str(c.get("sha", "")), str(match.get("sha", ""))
        kind = (
            "same commit"
            if c.get("same_commit")
            else ("same fix" if match.get("exact") else "overlap")
        )
        shared = str(match.get("shared", "?"))
        when = "this run" if match.get("source") == "run" else str(match.get("first_seen"))[:10]
        rows.append(
            (
                repo,
                _commit_link(sha, bases.get(repo), None),
                kind,
                other,
                _commit_link(other_sha, bases.get(other), None),
                shared,
                _num(match.get("overlap"), 2),
                when,
                str(c.get("subject", "")),
            )
        )
    text = (
        f"{len(collisions)} candidates duplicate or overlap a fix recorded under another "
        f"repository, earlier in this run or before it; {same} of them are the same commit "
        "(a fork or a mirror shares its history). Shared counts the distinct source and test "
        "hunks both fixes have; overlap divides it by the hunks of the smaller fix."
    )
    headers = (
        "repository",
        "commit",
        "kind",
        "matches",
        "commit",
        "shared",
        "overlap",
        "recorded",
        "subject",
    )
    return [Paragraph((text,)), Table(headers, tuple(rows), frozenset({5, 6}))]


def _best_new(batch: BatchExport, bases: dict[str, str | None], top: int | None) -> list[Block]:
    found = [
        candidate
        for export in batch.runs
        for candidate in export.candidates
        if outcome_of(candidate) == "new"
    ]
    if not found:
        return [Paragraph(("No new candidates.",))]
    found.sort(key=lambda c: (-float(c["score"]), -_stamp(c["date"]), str(c["sha"])))
    shown = found if top is None else found[:top]
    rows = tuple(
        (
            str(position),
            str(c["repo"]),
            str(c["rank"]),
            _commit_link(c["sha"], bases.get(str(c["repo"])), c.get("pull_request")),
            str(c["date"])[:10],
            _num(c["score"], 2),
            f"{_num(c['difficulty'].get('value'), 2)} {c['difficulty'].get('band', '?')}",
            str(c["subject"]),
        )
        for position, c in enumerate(shown, start=1)
    )
    text = (
        f"All {len(found)} new candidates"
        if len(shown) == len(found)
        else f"The best {len(shown)} of {len(found)} new candidates"
    )
    return [
        Paragraph((f"{text}, by score; rank is the rank within the repository's run.",)),
        Table(
            ("#", "repository", "rank", "commit", "date", "score", "difficulty", "subject"),
            rows,
            frozenset({0, 2, 5}),
        ),
    ]


def build_batch(batch: BatchExport, top: int | None = None) -> list[Block]:
    """The batch report: repositories, collisions, best new candidates, then each run."""
    record = batch.batch
    bases = {str(e.run["repo"]): _base(e.run) for e in batch.runs}
    walked = sum(int(e.run["walked"]) for e in batch.runs)
    candidates = sum(int(e.run["candidates"]) for e in batch.runs)
    new = sum(1 for e in batch.runs for c in e.candidates if outcome_of(c) == "new")
    flags = []
    if record["dry_run"]:
        flags.append("dry run: nothing was recorded")
    if record["full"]:
        flags.append("--full: the watermarks were ignored")
    flag = f"; {'; '.join(flags)}" if flags else ""
    failed = len(record["failed"])
    intro = (
        f"Batch file {record['config']}, ledger {record['ledger']} "
        f"(min overlap {record['min_overlap']}{flag}). {len(batch.runs)} runs"
        f"{f' ({failed} more failed)' if failed else ''}: {walked} commits and pull requests "
        f"walked, {candidates} candidates, {new} new, {len(record['collisions'])} colliding "
        f"with other repositories. CommitMiner {record['commitminer']}, export schema version "
        f"{record['schema_version']}."
    )
    blocks: list[Block] = [
        Heading(1, f"CommitMiner batch report: {record['config']}"),
        Paragraph((intro,)),
        Heading(2, "Repositories"),
        Paragraph(
            (
                "One row per repository and source, in batch order. Walked counts only what "
                "was new since the watermark; evaluated before counts what earlier runs had "
                "already seen.",
            )
        ),
        _repositories(batch),
        Heading(2, "Collisions across repositories"),
        *_collisions(batch, bases),
        Heading(2, "Best new candidates"),
        *_best_new(batch, bases, top),
    ]
    for export in batch.runs:
        run = export.run
        blocks.append(Heading(2, f"{run['repo']} ({run['source']})"))
        blocks += _run_blocks(export, top, 3)
    return blocks


# --- Markdown ---------------------------------------------------------------------------------


def _md(text: str) -> str:
    return _MD_SPECIAL.sub(r"\\\1", text).replace("\n", " ")


def _md_cell(cell: Cell) -> str:
    if isinstance(cell, Link):
        if cell.url and _HTTP.match(cell.url):
            return f"[{_md(cell.text)}]({cell.url.replace(')', '%29').replace(' ', '%20')})"
        return _md(cell.text)
    return _md(cell)


def _md_table(table: Table) -> list[str]:
    rows = ["| " + " | ".join(_md(h) for h in table.headers) + " |"]
    rows.append(
        "|"
        + "|".join(" ---: " if i in table.numeric else " --- " for i in range(len(table.headers)))
        + "|"
    )
    rows += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in table.rows]
    return rows


def _md_blocks(blocks: Iterable[Block]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            out += [f"{'#' * block.level} {_md(block.text)}", ""]
        elif isinstance(block, Paragraph):
            out += ["".join(_md_cell(part) for part in block.parts), ""]
        elif isinstance(block, Items):
            out += ["- " + "".join(_md_cell(part) for part in item) for item in block.items]
            out.append("")
        elif isinstance(block, Table):
            out += [*_md_table(block), ""]
        else:
            out += [f"{'#' * block.level} {_md(block.summary)}", "", *_md_blocks(block.body)]
    return out


def render_markdown(export: Export | BatchExport, top: int | None = None) -> str:
    """The report as Markdown (GitHub-flavoured tables)."""
    return "\n".join(_md_blocks(build(export, top))).rstrip("\n") + "\n"


# --- HTML -------------------------------------------------------------------------------------

_CSS: Final = """\
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 96rem; padding: 0 1rem;
       color: #1f2328; background: #ffffff; line-height: 1.4; }
h1, h2, h3 { line-height: 1.25; }
h2 { border-bottom: 1px solid #d0d7de; padding-bottom: 0.3rem; margin-top: 2rem; }
table { border-collapse: collapse; margin: 0.5rem 0 1rem; font-size: 0.9rem; }
th, td { border: 1px solid #d0d7de; padding: 0.25rem 0.5rem; text-align: left;
         vertical-align: top; }
th { background: #f6f8fa; }
td.n, th.n { text-align: right; font-variant-numeric: tabular-nums; }
tr:nth-child(even) td { background: #fbfbfb; }
details { margin: 0.5rem 0; }
summary { cursor: pointer; font-weight: 600; }
code, .sha { font-family: ui-monospace, monospace; }
a { color: #0969da; }
ul { padding-left: 1.5rem; }
"""


def _h(text: str) -> str:
    return html.escape(text, quote=True)


def _html_cell(cell: Cell) -> str:
    if isinstance(cell, Link):
        if cell.url and _HTTP.match(cell.url):
            return f'<a href="{_h(cell.url)}">{_h(cell.text)}</a>'
        return _h(cell.text)
    return _h(cell)


def _html_table(table: Table) -> list[str]:
    def klass(index: int) -> str:
        return ' class="n"' if index in table.numeric else ""

    out = ["<table>", "<thead><tr>"]
    out += [f"<th{klass(i)}>{_h(h)}</th>" for i, h in enumerate(table.headers)]
    out += ["</tr></thead>", "<tbody>"]
    for row in table.rows:
        cells = "".join(f"<td{klass(i)}>{_html_cell(c)}</td>" for i, c in enumerate(row))
        out.append(f"<tr>{cells}</tr>")
    out += ["</tbody>", "</table>"]
    return out


def _html_blocks(blocks: Iterable[Block]) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if isinstance(block, Heading):
            out.append(f"<h{block.level}>{_h(block.text)}</h{block.level}>")
        elif isinstance(block, Paragraph):
            out.append("<p>" + "".join(_html_cell(part) for part in block.parts) + "</p>")
        elif isinstance(block, Items):
            out.append("<ul>")
            out += [
                "<li>" + "".join(_html_cell(part) for part in item) + "</li>"
                for item in block.items
            ]
            out.append("</ul>")
        elif isinstance(block, Table):
            out += _html_table(block)
        else:
            out.append(f"<details><summary>{_h(block.summary)}</summary>")
            out += _html_blocks(block.body)
            out.append("</details>")
    return out


def render_html(export: Export | BatchExport, top: int | None = None) -> str:
    """The report as one self-contained HTML page (inline CSS, no scripts, everything escaped)."""
    title = (
        f"CommitMiner batch report: {export.batch['config']}"
        if isinstance(export, BatchExport)
        else f"CommitMiner report: {export.run['repo']}"
    )
    head = [
        "<!DOCTYPE html>",
        '<html lang="en">',
        "<head>",
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{_h(title)}</title>",
        "<style>",
        _CSS.rstrip("\n"),
        "</style>",
        "</head>",
        "<body>",
    ]
    return "\n".join([*head, *_html_blocks(build(export, top)), "</body>", "</html>"]) + "\n"


def format_for(out: Path | None, explicit: str | None) -> str:
    """The output format: ``--format``, else from the ``--out`` suffix, else Markdown."""
    if explicit is not None:
        if explicit not in FORMATS:
            raise ReportError(f"--format must be {' or '.join(FORMATS)}, not {explicit!r}")
        return explicit
    if out is not None:
        return _SUFFIXES.get(out.suffix.lower(), "markdown")
    return "markdown"


def render(export: Export | BatchExport, fmt: str, top: int | None = None) -> str:
    """Render in ``fmt`` (``markdown`` or ``html``), always encodable as UTF-8.

    Text that was not UTF-8 in git (lone surrogates in the export) shows as U+FFFD.
    """
    text = render_html(export, top) if fmt == "html" else render_markdown(export, top)
    return without_surrogates(text)
