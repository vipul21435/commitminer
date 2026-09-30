"""Mine several repositories into one ledger, resuming each from its watermark.

A batch file is a ``commitminer.toml`` with a ``[batch]`` table that lists the
repositories; its ``[classify]``, ``[filter]``, ``[score]`` and
``[difficulty]`` tables apply to every one of them. Relative paths are
resolved against the batch file's directory::

    [batch]
    ledger = ".commitminer/ledger.sqlite3"    # the default; created if missing
    out = "out/batch-candidates.jsonl"        # optional: the export
    report = "out/batch-report.md"            # optional: .md or .html
    min_overlap = 0.5                         # optional
    owner = "sourcing"                        # optional: owner of the proposed entries

    [[batch.repos]]                           # in order: earlier ones win collisions
    name = "hukkin/tomli"                     # the repository label in the ledger
    history = "examples/tomli/history.jsonl.gz"

    [[batch.repos]]
    name = "demo/durations"
    clone = "../durations"                    # a local clone
    rev = "main"                              # optional (HEAD); content = false for path rules only

    [[batch.repos]]
    name = "hukkin/tomli"
    github = "hukkin/tomli"                   # merged pull requests (GITHUB_TOKEN optional)
    limit = 25                                # optional (30); also max_files, max_wait,
    replay = "examples/tomli/prs"             # api_url, cache_dir, and replay for fixtures

Every entry may also set ``url`` (the repository URL written to the export)
and ``config`` (another ``commitminer.toml`` whose classifier and scoring
tables replace the batch file's for that entry). An entry is identified by its
name and source, so one repository can be listed once per source.

Every repository is walked first (what is new since its watermark). Then, in
one ledger transaction, each is filtered, ranked and recorded in order (new
fixes as ``proposed``, the walked shas, the new watermark; see
:meth:`commitminer.ledger.Ledger.record_run`), and the transaction commits
only after the export and the report are written: a batch that fails or is
interrupted before leaves the ledger as it was. How each source resumes:

- **clone**: the watermark is the head commit walked. A re-run walks
  ``watermark..head`` only; when the watermark is no longer an ancestor of the
  head (rewritten history) or not in the clone, everything is walked and the
  commits evaluated before are passed over.
- **history**: the watermark is the recording's head. Commits evaluated before
  are passed over, so replaying the same recording walks nothing and a newer
  recording of the same repository walks only its new commits.
- **pull requests**: the watermark is the newest ``updated_at`` listed. The
  listing (newest updated first) stops at the watermark, and pull requests
  whose commit was evaluated before are passed over without reading their
  files or commits.

A candidate that duplicates or overlaps a fix recorded under another
repository, earlier in the batch or in an earlier run, is a **collision**: the
same fix proposed from two repositories (a fork, a mirror, a vendored copy, a
backport). A repository that cannot be walked is reported and skipped; the
others still run.
"""

from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from commitminer import __version__
from commitminer.config import Config, ConfigError, load_config, parse_config
from commitminer.export import SCHEMA_VERSION, Resume, Run, export_records, reference
from commitminer.gitlog import GitError, is_ancestor, origin_url, resolve_commit, walk
from commitminer.history import HistoryError, read_history
from commitminer.ledger import (
    DEFAULT_MIN_OVERLAP,
    Ledger,
    Match,
    Proposal,
    Status,
    Verdict,
    Watermark,
    match_to_json,
)
from commitminer.models import Commit
from commitminer.scoring import Candidate, MineResult, mine
from commitminer.settings import Settings

if TYPE_CHECKING:
    from commitminer.github import GitHubClient

API_URL: Final = "https://api.github.com"
"""GitHub's REST API root (a copy of :data:`commitminer.github.API_URL`, which imports httpx)."""
DEFAULT_LEDGER: Final = ".commitminer/ledger.sqlite3"
DEFAULT_LIMIT: Final = 30
DEFAULT_MAX_FILES: Final = 300
DEFAULT_MAX_WAIT: Final = 300.0
REPO_NAME: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*)/[A-Za-z0-9._-]+$")
"""``OWNER/REPO`` (a copy of :data:`commitminer.pulls.REPO_NAME`)."""

_SOURCES: Final = {"clone": "clone", "history": "history", "github": "pull-requests"}
"""The key that names an entry's target, and the source it makes."""
_UNITS: Final = {"clone": "commits", "history": "commits", "pull-requests": "pull requests"}
_BATCH_KEYS: Final = frozenset({"ledger", "out", "report", "min_overlap", "owner", "repos"})
_COMMON_KEYS: Final = frozenset({"name", "url", "config"})
_SOURCE_KEYS: Final[dict[str, frozenset[str]]] = {
    "clone": frozenset({"rev", "content"}),
    "history": frozenset(),
    "pull-requests": frozenset(
        {"limit", "max_files", "max_wait", "api_url", "cache_dir", "replay"}
    ),
}


class SourceError(RuntimeError):
    """One repository of a batch could not be walked (git, recording or GitHub error)."""


@dataclass(frozen=True, slots=True)
class RepoSpec:
    """One ``[[batch.repos]]`` entry, with its paths resolved and its settings loaded."""

    name: str
    source: str
    """``clone``, ``history`` or ``pull-requests``."""
    target: str
    """The clone or recording path, or ``OWNER/REPO``."""
    settings: Settings
    config: Path | None = None
    url: str | None = None
    rev: str = "HEAD"
    content: bool = True
    limit: int = DEFAULT_LIMIT
    max_files: int = DEFAULT_MAX_FILES
    max_wait: float = DEFAULT_MAX_WAIT
    api_url: str | None = None
    cache_dir: Path | None = None
    replay: Path | None = None

    @property
    def unit(self) -> str:
        """``commits`` or ``pull requests``."""
        return _UNITS[self.source]

    @property
    def label(self) -> str:
        """``name [source]``, for messages."""
        return f"{self.name} [{self.source}]"


@dataclass(frozen=True, slots=True)
class BatchConfig:
    """A parsed batch file."""

    path: Path
    ledger: Path
    out: Path | None
    report: Path | None
    min_overlap: float
    owner: str | None
    repos: tuple[RepoSpec, ...]


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: expected a non-empty string")
    return value


def _path(value: Any, base: Path, where: str) -> Path:
    path = Path(_string(value, where)).expanduser()
    joined = path if path.is_absolute() else base / path
    # normpath drops "dir/.." lexically, so messages show examples/tomli, not batch/../tomli.
    # The OS resolves ".." after following a symlink, so the short form is kept only when
    # it names the same file; otherwise "dir/.." stays and the OS resolves it.
    short = os.path.normpath(joined)
    return Path(short) if os.path.realpath(short) == os.path.realpath(joined) else joined


def _integer(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where}: expected a positive integer, got {value!r}")
    return value


def _repo(raw: Any, base: Path, shared: Config, where: str) -> RepoSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"{where}: expected a table")
    kinds = [key for key in _SOURCES if key in raw]
    if len(kinds) != 1:
        raise ConfigError(f"{where}: give exactly one of {', '.join(_SOURCES)}")
    kind = kinds[0]
    source = _SOURCES[kind]
    unknown = sorted(set(raw) - _COMMON_KEYS - {kind} - _SOURCE_KEYS[source])
    if unknown:
        owners = [k for k, s in _SOURCES.items() if unknown[0] in _SOURCE_KEYS[s]]
        hint = f" (it applies to {owners[0]} entries)" if owners else ""
        raise ConfigError(f"{where}: unknown key {unknown[0]!r}{hint}")
    name = _string(raw.get("name"), f"{where}.name")
    target = _string(raw[kind], f"{where}.{kind}")
    if source == "pull-requests":
        if not REPO_NAME.match(target):
            raise ConfigError(f"{where}.github: {target!r} is not OWNER/REPO")
    else:
        target = str(_path(target, base, f"{where}.{kind}"))
    config_path = _path(raw["config"], base, f"{where}.config") if "config" in raw else None
    settings = (load_config(config_path) if config_path else shared).settings()
    options: dict[str, Any] = {}
    if "url" in raw:
        options["url"] = _string(raw["url"], f"{where}.url")
    if "rev" in raw:
        options["rev"] = _string(raw["rev"], f"{where}.rev")
    if "content" in raw:
        if not isinstance(raw["content"], bool):
            raise ConfigError(f"{where}.content: expected true or false")
        options["content"] = raw["content"]
    for key in ("limit", "max_files"):
        if key in raw:
            options[key] = _integer(raw[key], f"{where}.{key}")
    if "max_wait" in raw:
        wait = raw["max_wait"]
        if isinstance(wait, bool) or not isinstance(wait, int | float) or not 0 <= wait < 1e9:
            raise ConfigError(f"{where}.max_wait: expected seconds (0 or more), got {wait!r}")
        options["max_wait"] = float(wait)
    if "api_url" in raw:
        options["api_url"] = _string(raw["api_url"], f"{where}.api_url")
    for key in ("cache_dir", "replay"):
        if key in raw:
            options[key] = _path(raw[key], base, f"{where}.{key}")
    return RepoSpec(name, source, target, settings, config_path, **options)


def parse_batch(data: Mapping[str, Any], path: Path) -> BatchConfig:
    """Validate a parsed batch file: the shared tables, then ``[batch]`` and its repositories."""
    where = str(path)
    shared = parse_config(data, path)
    section = data.get("batch")
    if section is None:
        raise ConfigError(f"{where}: no [batch] table; list the repositories as [[batch.repos]]")
    if not isinstance(section, dict):
        raise ConfigError(f"{where}: [batch] must be a table")
    unknown = sorted(set(section) - _BATCH_KEYS)
    if unknown:
        allowed = ", ".join(sorted(_BATCH_KEYS))
        raise ConfigError(f"{where}: [batch]: unknown key {unknown[0]!r} (allowed: {allowed})")
    base = path.parent
    min_overlap = section.get("min_overlap", DEFAULT_MIN_OVERLAP)
    if (
        isinstance(min_overlap, bool)
        or not isinstance(min_overlap, int | float)
        or not 0 < min_overlap <= 1
    ):
        raise ConfigError(f"{where}: batch.min_overlap: expected a number in (0, 1]")
    owner = section.get("owner")
    raw_repos = section.get("repos")
    if not isinstance(raw_repos, list) or not raw_repos:
        raise ConfigError(f"{where}: batch.repos: list at least one repository ([[batch.repos]])")
    repos = tuple(
        _repo(raw, base, shared, f"{where}: batch.repos[{index}]")
        for index, raw in enumerate(raw_repos)
    )
    seen: set[tuple[str, str]] = set()
    for repo in repos:
        if (repo.name, repo.source) in seen:
            raise ConfigError(f"{where}: {repo.label} is listed twice")
        seen.add((repo.name, repo.source))
    return BatchConfig(
        path=path,
        ledger=_path(section.get("ledger", DEFAULT_LEDGER), base, f"{where}: batch.ledger"),
        out=_path(section["out"], base, f"{where}: batch.out") if "out" in section else None,
        report=_path(section["report"], base, f"{where}: batch.report")
        if "report" in section
        else None,
        min_overlap=float(min_overlap),
        owner=None if owner is None else _string(owner, f"{where}: batch.owner"),
        repos=repos,
    )


def load_batch(path: Path) -> BatchConfig:
    """Read and validate a batch file (see the module docstring)."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    return parse_batch(data, path)


# --- walking one repository -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Walked:
    """The new commits of one repository and how the walk resumed."""

    commits: tuple[Commit, ...]
    resume: Resume
    url: str | None
    notes: tuple[str, ...] = ()
    """Extra lines for the terminal (GitHub request counts, pull requests passed over)."""


def _mode(previous: str | None, full: bool, walked: int) -> str:
    if previous is None:
        return "first"
    if full:
        return "full"
    return "resumed" if walked else "up-to-date"


def _unseen(commits: Sequence[Commit], seen: Collection[str]) -> tuple[Commit, ...]:
    return tuple(commit for commit in commits if commit.sha not in seen)


def walk_clone(spec: RepoSpec, mark: Watermark | None, seen: Collection[str], full: bool) -> Walked:
    """Walk a clone from its watermark to the head of ``spec.rev``."""
    root = Path(spec.target)
    previous = mark.position if mark is not None and mark.position else None
    note = None
    try:
        head = resolve_commit(root, spec.rev)
        if previous is None or full:
            commits = walk(root, head, content=spec.content)
        elif previous == head:
            commits = []
        elif ancestor := is_ancestor(root, previous, head):
            commits = walk(root, f"{previous}..{head}", content=spec.content)
        else:
            where = "is not an ancestor of" if ancestor is False else "is not in the clone at"
            note = f"watermark {previous[:10]} {where} {head[:10]}; walked everything"
            commits = walk(root, head, content=spec.content)
        url = spec.url or origin_url(root)
    except (GitError, OSError) as exc:
        raise SourceError(f"{root}: {exc}") from exc
    kept = _unseen(commits, seen)
    mode = "full" if note is not None else _mode(previous, full, len(kept))
    return Walked(kept, Resume(mode, previous, head, len(commits) - len(kept), note), url)


def walk_history(
    spec: RepoSpec, mark: Watermark | None, seen: Collection[str], full: bool
) -> Walked:
    """Replay a recording, passing over the commits a batch evaluated before."""
    try:
        header, commits = read_history(Path(spec.target))
    except (HistoryError, OSError) as exc:
        raise SourceError(f"{spec.target}: {exc}") from exc
    previous = mark.position if mark is not None and mark.position else None
    position = header.head or (commits[0].sha if commits else None)
    kept = _unseen(commits, seen)
    resume = Resume(_mode(previous, full, len(kept)), previous, position, len(commits) - len(kept))
    return Walked(kept, resume, spec.url or header.url)


def api_root(spec: RepoSpec) -> str:
    """The REST API root of an entry: ``api_url``, else ``$GITHUB_API_URL``, else GitHub's."""
    return spec.api_url or os.environ.get("GITHUB_API_URL") or API_URL


def web_url(api_url: str, repo: str) -> str:
    """The repository's web URL from the REST API root: github.com, or a GHES host."""
    root = api_url.rstrip("/")
    if root == API_URL:
        return f"https://github.com/{repo}"
    return f"{root.removesuffix('/api/v3')}/{repo}"


ClientFactory = Callable[[RepoSpec], "GitHubClient"]
"""Builds the GitHub client of one pull-request entry (the CLI's uses the network or fixtures)."""


def walk_pulls(
    spec: RepoSpec,
    mark: Watermark | None,
    seen: Collection[str],
    full: bool,
    client_factory: ClientFactory,
) -> Walked:
    """List merged pull requests updated after the watermark, skipping evaluated ones."""
    from commitminer.github import GitHubError
    from commitminer.pulls import merged_pulls

    previous = mark.position if mark is not None and mark.position else None
    try:
        with client_factory(spec) as client:
            pulls = merged_pulls(
                client,
                spec.target,
                spec.limit,
                spec.max_files,
                since=None if full else previous,
                seen=seen,
            )
    except (GitHubError, OSError) as exc:
        # OSError: the response cache (cache_dir, or $XDG_CACHE_HOME) cannot be written.
        raise SourceError(f"{spec.target}: {exc}") from exc
    replayed = f" (replayed from {spec.replay})" if spec.replay is not None else ""
    notes = [f"github: {client.stats.describe()}{replayed}"]
    passed = []
    if pulls.closed_unmerged:
        passed.append(f"{pulls.closed_unmerged} closed without merging")
    if pulls.too_many_files:
        numbers = ", ".join(f"#{n}" for n in pulls.too_many_files)
        passed.append(f"{numbers} with more than {spec.max_files} changed files")
    if passed:
        notes.append(f"passed over: {'; '.join(passed)}")
    newest = [p for p in (previous, pulls.newest) if p is not None]
    position = max(newest) if newest else None
    mode = _mode(previous, full, len(pulls.commits))
    resume = Resume(mode, previous, position, pulls.already_walked)
    return Walked(
        pulls.commits, resume, spec.url or web_url(api_root(spec), spec.target), tuple(notes)
    )


# --- running a batch --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RepoRun:
    """The outcome of one repository: its walk, ranking and ledger verdicts, or an error."""

    spec: RepoSpec
    walked: Walked | None = None
    result: MineResult | None = None
    verdicts: tuple[Verdict, ...] = ()
    error: str | None = None


@dataclass(frozen=True, slots=True)
class Collision:
    """A candidate that duplicates or overlaps a fix recorded under another repository."""

    repo: str
    rank: int
    candidate: Candidate
    match: Match

    @property
    def status(self) -> Status:
        """``duplicate`` for the same fingerprint, else ``overlap``."""
        return Status.DUPLICATE if self.match.exact else Status.OVERLAP

    @property
    def same_commit(self) -> bool:
        """The other repository recorded this very commit (a fork shares its upstream's)."""
        return self.match.entry.sha == self.candidate.commit.sha


@dataclass(frozen=True, slots=True)
class BatchResult:
    """Every repository's run and the collisions across repositories."""

    config: BatchConfig
    ledger: Path
    runs: tuple[RepoRun, ...]
    collisions: tuple[Collision, ...]
    full: bool = False
    dry_run: bool = False

    @property
    def failed(self) -> tuple[RepoRun, ...]:
        """The repositories that could not be walked."""
        return tuple(run for run in self.runs if run.error is not None)


OUTCOMES: Final = ("new", "recorded", "internal", "collision", "unknown")
"""What the ledger said about a candidate, from a batch's point of view."""


def outcome(repo: str, sha: str, verdict: Verdict) -> str:
    """``new``; ``recorded`` (this commit is already in the ledger under this repository);
    ``collision`` (the fix is recorded under another repository); ``internal`` (a duplicate
    or overlap of another commit of the same repository, such as a cherry-pick onto a
    release branch); or ``unknown`` (no fingerprint)."""
    if verdict.status in (Status.NEW, Status.UNKNOWN):
        return verdict.status.value
    best = verdict.matches[0].entry
    if best.repo == repo and best.sha == sha:
        return "recorded"
    if any(match.entry.repo != repo for match in verdict.matches):
        return "collision"
    return "internal"


def _collisions(repo: str, result: MineResult, verdicts: Sequence[Verdict]) -> list[Collision]:
    found = []
    for rank, (candidate, verdict) in enumerate(
        zip(result.candidates, verdicts, strict=True), start=1
    ):
        if outcome(repo, candidate.commit.sha, verdict) != "collision":
            continue
        match = next(m for m in verdict.matches if m.entry.repo != repo)
        found.append(Collision(repo, rank, candidate, match))
    return found


def select(config: BatchConfig, only: Sequence[str]) -> tuple[RepoSpec, ...]:
    """The entries named by ``only`` (all when empty), in file order."""
    if not only:
        return config.repos
    names = {spec.name for spec in config.repos}
    missing = [name for name in only if name not in names]
    if missing:
        raise ConfigError(f"{config.path}: no repository named {missing[0]!r} in [[batch.repos]]")
    return tuple(spec for spec in config.repos if spec.name in only)


def run_batch(
    config: BatchConfig,
    ledger: Ledger,
    *,
    only: Sequence[str] = (),
    full: bool = False,
    dry_run: bool = False,
    client_factory: ClientFactory | None = None,
    progress: Callable[[RepoRun], None] | None = None,
    finish: Callable[[BatchResult], None] | None = None,
) -> BatchResult:
    """Mine each selected repository in order and record the batch in ``ledger``.

    Every repository is walked first, without holding the ledger's lock.
    Then, in one transaction, each is ranked and recorded in order (so later
    repositories collide with earlier ones), ``progress`` is called after
    each, and ``finish`` gets the result (the CLI writes the export and the
    report there). The transaction commits only when ``finish`` returns: if
    anything fails or is interrupted before, the ledger is left unchanged and
    the next run does the whole batch again, so no candidate is recorded
    without having been exported.

    ``full`` ignores the watermarks (everything is walked again; fixes already
    recorded under the same repository come back as ``recorded``).
    ``dry_run`` records into an in-memory copy of the ledger, so the verdicts
    are those of a real run and the file is left unchanged.
    """
    specs = select(config, only)
    target = ledger.snapshot() if dry_run else ledger
    path = ledger.path if ledger.path is not None else config.ledger
    try:
        walks = [(spec, _walk_one(spec, target, full, client_factory)) for spec in specs]
        runs: list[RepoRun] = []
        collisions: list[Collision] = []
        with target.transaction():
            for spec, walked in walks:
                run = _record_one(spec, walked, config, target)
                if run.result is not None:
                    collisions += _collisions(spec.name, run.result, run.verdicts)
                runs.append(run)
                if progress is not None:
                    progress(run)
            result = BatchResult(config, path, tuple(runs), tuple(collisions), full, dry_run)
            if finish is not None:
                finish(result)
    finally:
        if dry_run:
            target.close()
    return result


def _walk_one(
    spec: RepoSpec, ledger: Ledger, full: bool, client_factory: ClientFactory | None
) -> Walked | str:
    """What is new in one repository since its watermark, or why it cannot be walked."""
    mark = ledger.watermark(spec.name, spec.source)
    seen = frozenset() if full else ledger.walked_shas(spec.name, spec.source)
    try:
        if spec.source == "clone":
            return walk_clone(spec, mark, seen, full)
        if spec.source == "history":
            return walk_history(spec, mark, seen, full)
        if client_factory is None:
            raise SourceError("no GitHub client for pull-request entries")
        return walk_pulls(spec, mark, seen, full, client_factory)
    except SourceError as exc:
        return str(exc)


def _record_one(
    spec: RepoSpec, walked: Walked | str, config: BatchConfig, ledger: Ledger
) -> RepoRun:
    """Rank one walked repository and record its run in the ledger."""
    if isinstance(walked, str):
        return RepoRun(spec, error=walked)
    result = mine(walked.commits, spec.settings)
    proposals = [
        Proposal(spec.name, c.commit.sha, c.commit.subject, c.fingerprint)
        for c in result.candidates
    ]
    recorded = ledger.record_run(
        spec.name,
        spec.source,
        walked.resume.watermark or "",
        [commit.sha for commit in walked.commits],
        proposals,
        owner=config.owner,
        min_overlap=config.min_overlap,
    )
    return RepoRun(spec, walked, result, tuple(verdict for _, verdict in recorded))


# --- export -----------------------------------------------------------------------------------


def collision_to_json(collision: Collision) -> dict[str, Any]:
    """One collision of the batch record."""
    commit = collision.candidate.commit
    return {
        "repo": collision.repo,
        "sha": commit.sha,
        "subject": commit.subject,
        "rank": collision.rank,
        "status": collision.status.value,
        "same_commit": collision.same_commit,
        "match": match_to_json(collision.match),
    }


def batch_records(batch: BatchResult) -> list[dict[str, Any]]:
    """The batch export: the batch record, then each repository's run record and candidates."""
    records: list[dict[str, Any]] = [
        {
            "kind": "batch",
            "schema_version": SCHEMA_VERSION,
            "commitminer": __version__,
            "config": str(batch.config.path),
            "ledger": str(batch.ledger),
            "min_overlap": batch.config.min_overlap,
            "dry_run": batch.dry_run,
            "full": batch.full,
            "runs": sum(1 for run in batch.runs if run.result is not None),
            "failed": [
                {"repo": run.spec.name, "source": run.spec.source, "error": run.error}
                for run in batch.failed
            ],
            "collisions": [collision_to_json(c) for c in batch.collisions],
        }
    ]
    for run in batch.runs:
        if run.result is None or run.walked is None:
            continue
        spec = run.spec
        described = Run(
            repo=spec.name,
            url=run.walked.url,
            source=spec.source,
            unit=spec.unit,
            settings=spec.settings,
            ledger=str(batch.ledger),
            min_overlap=batch.config.min_overlap,
            resume=run.walked.resume,
        )
        records += export_records(
            run.result, spec.name, run.verdicts, run=described, all_verdicts=run.verdicts
        )
    return records


# --- terminal ---------------------------------------------------------------------------------


def short(position: str | None) -> str:
    """A watermark for the terminal: 10 digits of a sha, an update time in full, or ``-``."""
    if not position:
        return "-"
    return position[:10] if re.fullmatch(r"[0-9a-f]{40,64}", position) else position


def _count(count: int, unit: str) -> str:
    return f"{count} {unit.removesuffix('s') if count == 1 else unit}"


_OUTCOME_TEXT: Final = {
    "new": "new",
    "recorded": "already recorded",
    "internal": "matching other commits of this repository",
    "collision": "colliding with other repositories",
    "unknown": "without a fingerprint",
}


def outcome_counts(run: RepoRun) -> dict[str, int]:
    """Candidates per outcome (see :func:`outcome`), in :data:`OUTCOMES` order."""
    counts = dict.fromkeys(OUTCOMES, 0)
    if run.result is not None:
        for candidate, verdict in zip(run.result.candidates, run.verdicts, strict=True):
            counts[outcome(run.spec.name, candidate.commit.sha, verdict)] += 1
    return counts


def render_run(run: RepoRun) -> str:
    """One line per repository (and its notes): how it resumed, the funnel and the ledger."""
    spec = run.spec
    if run.walked is None or run.result is None:
        return f"{spec.label}: failed: {run.error}"
    resume, result = run.walked.resume, run.result
    how = {
        "first": "first run",
        "resumed": f"resumed from {short(resume.previous)}",
        "up-to-date": f"up to date at {short(resume.watermark)}",
        "full": "full walk",
    }[resume.mode]
    before = f" ({resume.skipped} evaluated before)" if resume.skipped else ""
    line = f"{spec.label}: {how}, walked {_count(result.walked, spec.unit)}{before}"
    if result.walked:
        line += (
            f": {_count(len(result.candidates), 'candidates')}, {len(result.rejections)} rejected"
        )
    counts = outcome_counts(run)
    ledger = ", ".join(f"{n} {_OUTCOME_TEXT[k]}" for k, n in counts.items() if n)
    if ledger:
        line += f"; {ledger}"
    if resume.mode != "up-to-date":
        line += f"; watermark {short(resume.watermark)}"
    notes = [f"  {resume.note}"] if resume.note else []
    notes += [f"  {note}" for note in run.walked.notes]
    return "\n".join([line, *notes])


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def render_collisions(batch: BatchResult) -> str:
    """The collision summary, each collision with another commit, and same-commit counts."""
    collisions = batch.collisions
    if not collisions:
        return "collisions with other repositories: none"
    same = [c for c in collisions if c.same_commit]
    others = [c for c in collisions if not c.same_commit]
    kinds = (
        (len(same), "same commit"),
        (sum(1 for c in others if c.match.exact), "same fix"),
        (sum(1 for c in others if not c.match.exact), "overlap"),
    )
    text = ", ".join(f"{count} {kind}" for count, kind in kinds if count)
    rows = [f"collisions with other repositories: {len(collisions)} ({text})"]
    for c in collisions:
        if c.same_commit:
            continue
        verdict = Verdict(c.status, (c.match,))
        rows.append(
            f"  {_ascii(c.repo)} {reference(c.candidate.commit)}  "
            f"{_ascii(verdict.describe(c.candidate.commit.sha, c.repo))}"
        )
    pairs: dict[tuple[str, str], int] = {}
    for c in same:
        key = (c.repo, c.match.entry.repo)
        pairs[key] = pairs.get(key, 0) + 1
    for (repo, other), count in pairs.items():
        rows.append(
            f"  {_count(count, 'candidates')} of {_ascii(repo)} "
            f"{'is a commit' if count == 1 else 'are commits'} of {_ascii(other)} too "
            "(a fork or a mirror shares its history)"
        )
    return "\n".join(rows)


def best_new(batch: BatchResult) -> list[tuple[RepoRun, int, Candidate]]:
    """The new candidates of every repository, best score first (then newest, then sha)."""
    found = []
    for run in batch.runs:
        if run.result is None:
            continue
        for rank, (candidate, verdict) in enumerate(
            zip(run.result.candidates, run.verdicts, strict=True), start=1
        ):
            if verdict.status is Status.NEW:
                found.append((run, rank, candidate))

    def key(item: tuple[RepoRun, int, Candidate]) -> tuple[float, float, str]:
        candidate = item[2]
        stamp = datetime.fromisoformat(candidate.commit.date).timestamp()
        return (-candidate.score, -stamp, candidate.commit.sha)

    return sorted(found, key=key)


def render_best(batch: BatchResult, top: int) -> str:
    """The ``top`` best new candidates across the batch, as a fixed-width table."""
    found = best_new(batch)
    if not found:
        return "no new candidates"
    width = max(len(_ascii(run.spec.name)) for run, _, _ in found[:top])
    rows = [
        f"best new candidates across the batch ({min(top, len(found))} of {len(found)}):",
        f"{'score':>6}  {'diff':>11}  {'repo':<{width}}  {'rank':>4}  {'commit':<10}  "
        f"{'date':<10}  subject",
    ]
    for run, rank, c in found[:top]:
        band = f"{c.difficulty.value:.2f} {c.difficulty.band}"
        subject = _ascii(c.commit.subject)
        subject = subject if len(subject) <= 50 else subject[:47] + "..."
        rows.append(
            f"{c.score:>6.2f}  {band:>11}  {_ascii(run.spec.name):<{width}}  {rank:>4}  "
            f"{reference(c.commit):<10}  {c.commit.date[:10]:<10}  {subject}"
        )
    return "\n".join(rows)
