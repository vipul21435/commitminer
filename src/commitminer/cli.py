"""Command-line interface for CommitMiner."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import typer

from commitminer import __version__
from commitminer.batch import (
    API_URL,
    BatchConfig,
    BatchResult,
    RepoRun,
    RepoSpec,
    api_root,
    batch_records,
    load_batch,
    render_best,
    render_collisions,
    render_run,
    resolve_api_root,
    run_batch,
    web_url,
)
from commitminer.config import Config, ConfigError, find_config, load_config
from commitminer.explain import ExplainError, explain_json, find_commit, render_commit
from commitminer.export import (
    Run,
    render_explanation,
    render_ledger,
    render_summary,
    render_table,
    schema_text,
    write_jsonl,
    write_records,
)
from commitminer.gitlog import GitError, head_sha, origin_url, resolve_commit, walk
from commitminer.history import HistoryError, read_history, write_history
from commitminer.ledger import (
    DEFAULT_MIN_OVERLAP,
    STATUSES,
    Ledger,
    LedgerError,
    Proposal,
    Status,
    Verdict,
    check_all,
    entry_to_json,
    ledger_verdict_to_json,
    open_ledger,
    read_candidates,
    watermark_to_json,
)
from commitminer.models import Commit
from commitminer.report import (
    FORMATS,
    ReportError,
    format_for,
    from_records,
    read_export,
    render,
)
from commitminer.ruletable import (
    classify_file,
    render_rules,
    render_rules_markdown,
    render_verdicts,
    verdict_to_json,
)
from commitminer.scoring import Candidate, MineResult, evaluate, mine
from commitminer.settings import Settings

if TYPE_CHECKING:
    import httpx

    from commitminer.github import GitHubClient

# The GitHub client (and httpx) is imported by the prs and batch commands only, so
# the other commands start faster; tests check that these copies (API_URL comes
# from commitminer.batch) match the modules' values.
DEFAULT_MAX_FILES = 300

app = typer.Typer(
    name="commitminer",
    help="Mine and rank candidate fail-to-pass tasks from repository history.",
    no_args_is_help=True,
    add_completion=False,
)


@app.callback()
def _root() -> None:
    """Mine and rank candidate fail-to-pass tasks from repository history."""


@app.command()
def version() -> None:
    """Print the CommitMiner version."""
    typer.echo(f"commitminer {__version__}")


EXIT_ERROR = 2
"""Exit code of every failure (bad input, git, GitHub or SQLite errors), as for usage errors.

Exit code 1 is an outcome, not an error: ``ledger add`` refused a candidate, or
``ledger check`` found one that is not new.
"""


def _fail(message: str, code: int = EXIT_ERROR) -> typer.Exit:
    typer.echo(f"error: {message}", err=True)
    return typer.Exit(code=code)


RevOption = Annotated[
    str, typer.Option("--rev", help="Revision or range to walk, as git log takes it.")
]
MaxCountOption = Annotated[
    int | None, typer.Option("--max-count", min=1, help="Walk at most this many commits.")
]
RepoNameOption = Annotated[
    str | None,
    typer.Option("--repo-name", help="Label for the repository in the output."),
]
ContentOption = Annotated[
    bool,
    typer.Option(
        "--content/--no-content",
        help="Read changed code files with git cat-file for content signals.",
    ),
]
ConfigOption = Annotated[
    Path | None,
    typer.Option(
        "--config",
        help="Classifier rules, limits, weights and bands "
        "(default: commitminer.toml at the repository root).",
        show_default=False,
    ),
]
RootOption = Annotated[
    Path,
    typer.Option(
        "--root",
        help="Repository root: relative paths are resolved against it and its "
        "commitminer.toml is used.",
    ),
]


MaxLinesOption = Annotated[
    int | None,
    typer.Option(
        "--max-lines",
        min=1,
        help="Largest source+test diff to accept (default: commitminer.toml, else 400).",
        show_default=False,
    ),
]
MaxSourceFilesOption = Annotated[
    int | None,
    typer.Option(
        "--max-source-files",
        min=1,
        help="Most source files a commit may change (default: commitminer.toml, else 10).",
        show_default=False,
    ),
]
TestLinesCapOption = Annotated[
    int | None,
    typer.Option(
        "--test-lines-cap",
        min=1,
        help="Added test lines for a full test score (default: commitminer.toml, else 40).",
        show_default=False,
    ),
]


LedgerOption = Annotated[
    Path | None,
    typer.Option(
        "--ledger",
        help="SQLite ledger to check candidates against (read only; must exist).",
        show_default=False,
    ),
]
MinOverlapOption = Annotated[
    float,
    typer.Option(
        "--min-overlap",
        min=0.01,
        max=1.0,
        help="Share of the smaller hunk set that makes two fixes overlap.",
    ),
]


@contextmanager
def _ledger(path: Path, create: bool = False, readonly: bool = False) -> Iterator[Ledger]:
    """Open a ledger for one command; any ledger error is a plain message and exit 2.

    ``readonly`` for commands that only read: the file is never written, not even to
    migrate an older schema (see :func:`commitminer.ledger.open_ledger`).
    """
    try:
        with open_ledger(path, create=create, readonly=readonly) as opened:
            yield opened
    except LedgerError as exc:
        raise _fail(str(exc)) from exc


def _proposal(candidate: Candidate, repo: str) -> Proposal:
    commit = candidate.commit
    return Proposal(repo, commit.sha, commit.subject, candidate.fingerprint)


def _config(explicit: Path | None, root: Path | None) -> Config:
    """Load ``--config``, else ``root/commitminer.toml`` if present, else the defaults."""
    path = explicit if explicit is not None else (find_config(root) if root else None)
    if path is None:
        return Config()
    try:
        return load_config(path)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc


@app.command(name="mine")
def mine_command(
    repo: Annotated[
        Path | None,
        typer.Argument(help="Local clone to walk. Omit when using --history.", show_default=False),
    ] = None,
    history: Annotated[
        Path | None,
        typer.Option("--history", help="Replay a recorded history instead of walking a clone."),
    ] = None,
    rev: RevOption = "HEAD",
    max_count: MaxCountOption = None,
    max_lines: MaxLinesOption = None,
    max_source_files: MaxSourceFilesOption = None,
    test_lines_cap: TestLinesCapOption = None,
    top: Annotated[int, typer.Option("--top", min=0, help="Rows to print in the table.")] = 10,
    explain: Annotated[
        int, typer.Option("--explain", min=0, help="Print the score breakdown of the best N.")
    ] = 1,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write all candidates to this JSONL file.")
    ] = None,
    repo_name: RepoNameOption = None,
    config: ConfigOption = None,
    content: ContentOption = True,
    ledger: LedgerOption = None,
    min_overlap: MinOverlapOption = DEFAULT_MIN_OVERLAP,
    new_only: Annotated[
        bool,
        typer.Option(
            "--new-only", help="With --ledger, leave duplicates and overlaps out of the output."
        ),
    ] = False,
    url: Annotated[
        str | None,
        typer.Option(
            "--url",
            help="Repository URL written to the export (default: the recording's URL, "
            "or the clone's origin remote).",
            show_default=False,
        ),
    ] = None,
) -> None:
    """Filter and rank the commits of a clone or a recorded history."""
    if new_only and ledger is None:
        raise _fail("--new-only needs --ledger")
    if (repo is None) == (history is None):
        # Plain text on purpose: Typer's rich usage panel re-wraps messages by terminal width.
        raise _fail("give either a REPO path or --history FILE, not both or neither")
    settings_config = _config(config, repo)
    commits: list[Commit]
    try:
        if history is not None:
            header, commits = read_history(history)
            label = repo_name or header.repo
            url = url or header.url
        else:
            assert repo is not None
            commits = walk(repo, rev, max_count, content)
            label = repo_name or repo.resolve().name
            url = url or origin_url(repo)
    except (GitError, HistoryError, OSError) as exc:
        raise _fail(str(exc)) from exc
    if history is not None and max_count is not None:
        commits = commits[:max_count]
    settings = settings_config.settings(
        max_lines=max_lines, max_source_files=max_source_files, test_lines_cap=test_lines_cap
    )
    if settings_config.path is not None:
        typer.echo(settings_config.describe())
    source = "history" if history is not None else "clone"
    _report(
        commits,
        label,
        settings,
        _Output(top, explain, out, ledger, min_overlap, new_only, url, source),
    )


@dataclass(frozen=True, slots=True)
class _Output:
    """What ``mine`` and ``prs`` print and write after ranking."""

    top: int
    explain: int
    out: Path | None
    ledger: Path | None
    min_overlap: float
    new_only: bool
    url: str | None
    source: str


def _report(
    commits: list[Commit], label: str, settings: Settings, output: _Output, unit: str = "commits"
) -> None:
    """Rank ``commits``, check them against the ledger, print the results and write JSONL."""
    result = mine(commits, settings)
    ledger, new_only = output.ledger, output.new_only
    verdicts: list[Verdict] | None = None
    if ledger is not None:
        with _ledger(ledger, readonly=True) as opened:
            proposals = [_proposal(c, label) for c in result.candidates]
            verdicts = check_all(opened, proposals, output.min_overlap)
    typer.echo(render_summary(result, label, unit))
    shown, ranks, all_verdicts = result, list(range(1, len(result.candidates) + 1)), verdicts
    if verdicts is not None:
        typer.echo(render_ledger(result, verdicts, str(ledger), repo=label))
        if new_only:
            kept = [i for i, v in enumerate(verdicts) if v.status is Status.NEW]
            shown = replace(result, candidates=tuple(result.candidates[i] for i in kept))
            verdicts = [verdicts[i] for i in kept]
            ranks = [i + 1 for i in kept]
            typer.echo(f"showing the {len(kept)} new candidates (--new-only)")
    _print_candidates(shown, ranks, output.top, output.explain, verdicts)
    if output.out is not None:
        run = Run(
            repo=label,
            url=output.url,
            source=output.source,
            unit=unit,
            settings=settings,
            ledger=None if ledger is None else str(ledger),
            min_overlap=None if ledger is None else output.min_overlap,
            new_only=new_only,
        )
        count = write_jsonl(output.out, shown, label, verdicts, ranks, run, result, all_verdicts)
        typer.echo("")
        typer.echo(f"wrote {count} candidates to {output.out}")


def _print_candidates(
    result: MineResult,
    ranks: list[int],
    top: int,
    explain: int,
    verdicts: list[Verdict] | None,
) -> None:
    """The table (ranked among all candidates) and the best ``explain`` breakdowns."""
    if top and result.candidates:
        typer.echo("")
        typer.echo(render_table(result, top, verdicts=verdicts, ranks=ranks))
    for rank, candidate in zip(ranks[:explain], result.candidates[:explain], strict=True):
        typer.echo("")
        typer.echo(render_explanation(candidate, rank))


def _network_transport() -> httpx.BaseTransport:
    """The real network (the tests replace it)."""
    import httpx

    return httpx.HTTPTransport()


@app.command(name="prs")
def prs_command(
    repo: Annotated[
        str, typer.Argument(help="GitHub repository as OWNER/REPO.", show_default=False)
    ],
    limit: Annotated[
        int,
        typer.Option("--limit", min=1, help="Merged pull requests to read, newest updated first."),
    ] = 30,
    max_files: Annotated[
        int,
        typer.Option(
            "--max-files",
            min=1,
            help="Skip pull requests with more changed files than this (read 100 per request).",
        ),
    ] = DEFAULT_MAX_FILES,
    record_dir: Annotated[
        Path | None,
        typer.Option("--record", help="Save every API response as a fixture in this directory."),
    ] = None,
    replay_dir: Annotated[
        Path | None,
        typer.Option("--replay", help="Answer every request from fixtures; no network."),
    ] = None,
    cache_dir: Annotated[
        Path | None,
        typer.Option(
            "--cache-dir",
            help="ETag cache for conditional requests (default: ~/.cache/commitminer/github; "
            "none with --replay unless given).",
            show_default=False,
        ),
    ] = None,
    no_cache: Annotated[
        bool, typer.Option("--no-cache", help="Neither read nor write the ETag cache.")
    ] = False,
    api_url: Annotated[
        str | None,
        typer.Option(
            "--api-url",
            help=f"REST API root (default: $GITHUB_API_URL, else {API_URL}; "
            "--replay ignores $GITHUB_API_URL).",
            show_default=False,
        ),
    ] = None,
    max_wait: Annotated[
        float,
        typer.Option(
            "--max-wait", min=0, help="Longest rate-limit wait in seconds before giving up."
        ),
    ] = 300.0,
    max_lines: MaxLinesOption = None,
    max_source_files: MaxSourceFilesOption = None,
    test_lines_cap: TestLinesCapOption = None,
    top: Annotated[int, typer.Option("--top", min=0, help="Rows to print in the table.")] = 10,
    explain: Annotated[
        int, typer.Option("--explain", min=0, help="Print the score breakdown of the best N.")
    ] = 1,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write all candidates to this JSONL file.")
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option(
            "--config", help="Classifier rules, limits, weights and bands.", show_default=False
        ),
    ] = None,
    ledger: LedgerOption = None,
    min_overlap: MinOverlapOption = DEFAULT_MIN_OVERLAP,
    new_only: Annotated[
        bool,
        typer.Option(
            "--new-only", help="With --ledger, leave duplicates and overlaps out of the output."
        ),
    ] = False,
) -> None:
    """Rank the merged pull requests of a GitHub repository (GITHUB_TOKEN is optional)."""
    if new_only and ledger is None:
        raise _fail("--new-only needs --ledger")
    if record_dir is not None and replay_dir is not None:
        raise _fail("give --record or --replay, not both")
    if record_dir is not None and cache_dir is not None:
        raise _fail("--record fetches every response in full; drop --cache-dir")
    from commitminer.fixtures import RecordingTransport, ReplayTransport
    from commitminer.github import (
        GitHubClient,
        GitHubError,
        ResponseCache,
        default_cache_dir,
    )
    from commitminer.pulls import merged_pulls

    loaded = _config(config, None)
    settings = loaded.settings(
        max_lines=max_lines, max_source_files=max_source_files, test_lines_cap=test_lines_cap
    )
    cache = None
    if not no_cache and record_dir is None and (cache_dir is not None or replay_dir is None):
        cache = ResponseCache(cache_dir or default_cache_dir())
    transport: httpx.BaseTransport
    recorder = None
    root = resolve_api_root(api_url, replaying=replay_dir is not None)
    try:
        if replay_dir is not None:
            transport = ReplayTransport(replay_dir)
        elif record_dir is not None:
            transport = recorder = RecordingTransport(_network_transport(), record_dir)
        else:
            transport = _network_transport()
        client = GitHubClient(
            token=os.environ.get("GITHUB_TOKEN") or None,
            api_url=root,
            cache=cache,
            transport=transport,
            max_wait=max_wait,
        )
        with client:
            walked = merged_pulls(client, repo, limit, max_files)
    except GitHubError as exc:
        raise _fail(str(exc)) from exc
    source = f" (replayed from {replay_dir})" if replay_dir is not None else ""
    typer.echo(f"github: {client.stats.describe()}{source}")
    if recorder is not None:
        typer.echo(f"recorded {recorder.recorded} responses to {record_dir}")
    skipped = ""
    if walked.too_many_files:
        numbers = ", ".join(f"#{number}" for number in walked.too_many_files)
        skipped = f"; skipped {numbers}: more than {max_files} changed files"
    typer.echo(
        f"{repo}: read {len(walked.commits)} merged pull requests "
        f"({walked.closed_unmerged} closed without merging passed over{skipped})"
    )
    if loaded.path is not None:
        typer.echo(loaded.describe())
    output = _Output(
        top, explain, out, ledger, min_overlap, new_only, web_url(root, repo), "pull-requests"
    )
    _report(list(walked.commits), repo, settings, output, unit="pull requests")


def _batch_client(spec: RepoSpec) -> GitHubClient:
    """The GitHub client of a batch's pull-request entry: fixtures with ``replay``, else live."""
    from commitminer.fixtures import ReplayTransport
    from commitminer.github import GitHubClient, ResponseCache, default_cache_dir

    cache = None
    if spec.cache_dir is not None or spec.replay is None:
        cache = ResponseCache(spec.cache_dir or default_cache_dir())
    transport = ReplayTransport(spec.replay) if spec.replay is not None else _network_transport()
    return GitHubClient(
        token=os.environ.get("GITHUB_TOKEN") or None,
        api_url=api_root(spec),
        cache=cache,
        transport=transport,
        max_wait=spec.max_wait,
    )


@app.command(name="batch")
def batch_command(
    config: Annotated[
        Path,
        typer.Argument(
            help="Batch file: a commitminer.toml with [batch] and [[batch.repos]].",
            show_default=False,
        ),
    ],
    ledger: Annotated[
        Path | None,
        typer.Option(
            "--ledger",
            help="Ledger to record into (default: batch.ledger, else "
            ".commitminer/ledger.sqlite3 next to the batch file; created if missing).",
            show_default=False,
        ),
    ] = None,
    out: Annotated[
        Path | None,
        typer.Option(
            "--out", help="Write the batch export here (default: batch.out).", show_default=False
        ),
    ] = None,
    report: Annotated[
        Path | None,
        typer.Option(
            "--report",
            help="Write the batch report here, Markdown or HTML by suffix (default: batch.report).",
            show_default=False,
        ),
    ] = None,
    only: Annotated[
        list[str] | None,
        typer.Option("--only", help="Run only the repositories with this name (repeatable)."),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="Ignore the watermarks and walk every repository in full."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Check against the ledger without recording anything in it."
        ),
    ] = False,
    top: Annotated[
        int, typer.Option("--top", min=0, help="Best new candidates to list across the batch.")
    ] = 10,
) -> None:
    """Mine several repositories into one ledger, walking only what is new since the last run."""
    try:
        batch = load_batch(config)
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    where = ledger or batch.ledger
    mode = " (dry run: the ledger is not changed)" if dry_run else ""
    count = len(batch.repos)
    repositories = "1 repository" if count == 1 else f"{count} repositories"
    typer.echo(f"batch {config}: {repositories}, ledger {where}{mode}")

    def finish(result: BatchResult) -> None:
        # Runs inside the ledger's transaction: if a write fails, nothing is recorded.
        typer.echo(render_collisions(result))
        if top:
            typer.echo("")
            typer.echo(render_best(result, top))
        records = batch_records(result)
        target = out or batch.out
        if target is not None:
            try:
                write_records(target, records)
            except OSError as exc:
                raise _fail(f"{target}: cannot write: {exc}{unchanged}") from exc
            runs = sum(1 for record in records if record["kind"] == "run")
            count = sum(1 for record in records if record["kind"] == "candidate")
            typer.echo("")
            typer.echo(f"wrote {runs} runs and {count} candidates to {target}")
        document = report or batch.report
        if document is not None:
            chosen = format_for(document, None)
            text = render(from_records(records, "the batch export"), chosen)
            try:
                document.parent.mkdir(parents=True, exist_ok=True)
                document.write_text(text, encoding="utf-8", newline="\n")
            except OSError as exc:
                raise _fail(f"{document}: cannot write: {exc}{unchanged}") from exc
            typer.echo(f"wrote the {chosen} report to {document}")

    unchanged = "" if dry_run else "; nothing was recorded in the ledger"
    result = _run_batch(batch, where, only or [], full, dry_run, finish)
    if result.failed:
        names = ", ".join(run.spec.label for run in result.failed)
        raise _fail(f"{len(result.failed)} of {len(result.runs)} repositories failed: {names}")


def _run_batch(
    batch: BatchConfig,
    where: Path,
    only: list[str],
    full: bool,
    dry_run: bool,
    finish: Callable[[BatchResult], None],
) -> BatchResult:
    """Open (or, for a dry run of a missing ledger, stand in for) the ledger and run the batch.

    ``finish`` writes the export and the report before the ledger commits.
    """

    def progress(run: RepoRun) -> None:
        typer.echo(render_run(run))

    path = where
    try:
        with ExitStack() as stack:
            if dry_run and not where.exists():
                path = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "empty.sqlite3"
            elif not where.parent.is_dir():
                try:
                    where.parent.mkdir(parents=True)
                except OSError as exc:
                    raise _fail(f"{where}: cannot create the ledger's directory: {exc}") from exc
            # A dry run reads the file only (read-only, never migrated) and records into
            # an in-memory copy; a missing ledger is stood in for by an empty one.
            opened = stack.enter_context(
                _ledger(path, create=True, readonly=path == where and dry_run)
            )
            result = run_batch(
                batch,
                opened,
                only=only,
                full=full,
                dry_run=dry_run,
                client_factory=_batch_client,
                progress=progress,
                finish=lambda result: finish(replace(result, ledger=where)),
            )
    except ConfigError as exc:
        raise _fail(str(exc)) from exc
    return replace(result, ledger=where)


@app.command()
def schema() -> None:
    """Print the JSON Schema of the export records written by mine --out and prs --out."""
    typer.echo(schema_text(), nl=False)


@app.command(name="report")
def report_command(
    candidates: Annotated[
        Path,
        typer.Argument(
            help="Export written by mine --out, prs --out or batch --out.", show_default=False
        ),
    ],
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write the report here (default: print it)."),
    ] = None,
    fmt: Annotated[
        str | None,
        typer.Option(
            "--format",
            help=f"{' or '.join(FORMATS)} (default: from the --out suffix, else markdown).",
            show_default=False,
        ),
    ] = None,
    top: Annotated[
        int | None,
        typer.Option("--top", min=1, help="Candidates in the ranked table (default: all)."),
    ] = None,
) -> None:
    """Render an export (of one run or of a batch) as a Markdown or self-contained HTML report."""
    try:
        chosen = format_for(out, fmt)
        text = render(read_export(candidates), chosen, top)
    except ReportError as exc:
        raise _fail(str(exc)) from exc
    if out is None:
        typer.echo(text, nl=False)
        return
    _write_report(out, text)
    typer.echo(f"wrote the {chosen} report to {out}")


def _write_report(out: Path, text: str) -> None:
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise _fail(f"{out}: cannot write: {exc}") from exc


@app.command()
def record(
    repo: Annotated[Path, typer.Argument(help="Local clone to record.")],
    out: Annotated[Path, typer.Option("--out", help="Recording to write (.jsonl or .jsonl.gz).")],
    rev: Annotated[
        str, typer.Option("--rev", help="Single revision to record the history of.")
    ] = "HEAD",
    max_count: MaxCountOption = None,
    repo_name: RepoNameOption = None,
    url: Annotated[
        str | None, typer.Option("--url", help="Source URL stored in the header.")
    ] = None,
    content: ContentOption = True,
) -> None:
    """Walk a clone once and save its history for offline replay with mine --history."""
    try:
        head = head_sha(repo, rev)
        commits = walk(repo, head, max_count, content)
    except GitError as exc:
        raise _fail(str(exc)) from exc
    header = write_history(out, commits, repo=repo_name or repo.resolve().name, url=url, head=head)
    typer.echo(f"recorded {header.commits} commits of {header.repo} at {head[:12]} to {out}")


def _explained_commit(
    sha: str, repo: Path | None, history: Path | None, content: bool
) -> tuple[Commit, str]:
    """Find the commit to explain in a recording or walk it in a clone; return it and a label."""
    try:
        if history is not None:
            header, commits = read_history(history)
            return find_commit(commits, sha), header.repo
        root = repo if repo is not None else Path()
        resolved = resolve_commit(root, sha)
        walked = walk(root, resolved, max_count=1, content=content)
    except (GitError, HistoryError, ExplainError, OSError) as exc:
        raise _fail(str(exc)) from exc
    if not walked or walked[0].sha != resolved:
        raise _fail(f"{sha} is a merge commit; merges are not candidates")
    return walked[0], root.resolve().name


@app.command(name="explain")
def explain_command(
    sha: Annotated[
        str,
        typer.Argument(
            help="Commit to explain: a sha (prefix) or, in a clone, any revision.",
            show_default=False,
        ),
    ],
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Local clone (default: the current directory)."),
    ] = None,
    history: Annotated[
        Path | None,
        typer.Option("--history", help="Look the commit up in a recorded history instead."),
    ] = None,
    max_lines: MaxLinesOption = None,
    max_source_files: MaxSourceFilesOption = None,
    test_lines_cap: TestLinesCapOption = None,
    repo_name: RepoNameOption = None,
    config: ConfigOption = None,
    content: ContentOption = True,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print the explanation as one JSON object.")
    ] = False,
) -> None:
    """Explain one commit: its files, the filter verdict, score and difficulty contributions."""
    if repo is not None and history is not None:
        raise _fail("give --repo or --history, not both")
    loaded = _config(config, (repo or Path()) if history is None else None)
    settings = loaded.settings(
        max_lines=max_lines, max_source_files=max_source_files, test_lines_cap=test_lines_cap
    )
    commit, label = _explained_commit(sha, repo, history, content)
    outcome = evaluate(commit, settings)
    if as_json:
        record = explain_json(outcome, settings, repo_name or label)
        typer.echo(json.dumps(record, sort_keys=True, ensure_ascii=True))
        return
    if loaded.path is not None:
        typer.echo(loaded.describe())
    typer.echo(render_commit(outcome, settings))


def _relative(root: Path, raw: str) -> str:
    """A repository-relative POSIX path for a CLI argument."""
    path = Path(raw)
    if path.is_absolute():
        try:
            path = path.resolve().relative_to(root.resolve())
        except ValueError:
            raise _fail(f"{raw} is outside the root {root}") from None
    return path.as_posix()


@app.command(name="classify")
def classify_command(
    paths: Annotated[
        list[str],
        typer.Argument(help="Paths relative to --root (files need not exist).", show_default=False),
    ],
    root: RootOption = Path(),
    config: ConfigOption = None,
    content: Annotated[
        bool,
        typer.Option("--content/--no-content", help="Read existing files for content signals."),
    ] = True,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON object per path.")] = (
        False
    ),
) -> None:
    """Show the category of each path and the rule that decided it."""
    rules = _config(config, root).rules
    verdicts = []
    for raw in paths:
        relative = _relative(root, raw)
        if (root / relative).is_dir():
            raise _fail(f"{raw} is a directory; pass file paths")
        verdicts.append(classify_file(root, relative, rules, content))
    if as_json:
        for verdict in verdicts:
            typer.echo(json.dumps(verdict_to_json(verdict), sort_keys=True, ensure_ascii=True))
        return
    typer.echo(render_verdicts(verdicts, content))


@app.command(name="rules")
def rules_command(
    root: RootOption = Path(),
    config: ConfigOption = None,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Print a Markdown table (docs/rules.md).")
    ] = False,
) -> None:
    """Print the classifier rule table in match order, including commitminer.toml overrides."""
    loaded = _config(config, root)
    if markdown:
        typer.echo(render_rules_markdown(loaded.rules))
        return
    if loaded.path is not None:
        typer.echo(loaded.describe())
    typer.echo(render_rules(loaded.rules, custom=len(loaded.custom_rules)))


ledger_app = typer.Typer(
    help="Record proposed fixes in a SQLite ledger and check candidates against it.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(ledger_app, name="ledger")

LedgerArgument = Annotated[Path, typer.Argument(help="Ledger file (SQLite).", show_default=False)]
NewLedgerArgument = Annotated[
    Path, typer.Argument(help="Ledger file (SQLite; created if missing).", show_default=False)
]
CandidatesArgument = Annotated[
    Path,
    typer.Argument(help="Candidates exported by mine --out (JSON Lines).", show_default=False),
]


def _read_candidates(path: Path) -> list[tuple[int | None, Proposal]]:
    try:
        return [(item.rank, item.proposal) for item in read_candidates(path)]
    except LedgerError as exc:
        raise _fail(str(exc)) from exc


def _label(rank: int | None, proposal: Proposal) -> str:
    where = f"#{rank} " if rank is not None else ""
    return f"{where}{proposal.repo} {proposal.sha[:10]}"


@ledger_app.command(name="add")
def ledger_add(
    ledger: NewLedgerArgument,
    candidates: CandidatesArgument,
    sha: Annotated[
        list[str] | None,
        typer.Option("--sha", help="Add only this candidate (sha prefix; repeatable)."),
    ] = None,
    top: Annotated[
        int | None, typer.Option("--top", min=1, help="Add only the first N candidates.")
    ] = None,
    status: Annotated[
        str, typer.Option("--status", help=f"Entry status: {' or '.join(STATUSES)}.")
    ] = "claimed",
    owner: Annotated[
        str | None, typer.Option("--owner", help="Who claims the fixes (free text).")
    ] = None,
    min_overlap: MinOverlapOption = DEFAULT_MIN_OVERLAP,
    allow_overlap: Annotated[
        bool,
        typer.Option("--allow-overlap", help="Refuse only exact duplicates, not partial overlaps."),
    ] = False,
) -> None:
    """Record candidates, refusing fixes the ledger holds (exit 1 if refused).

    A claim of a fix the ledger holds as proposed (as batch runs record them)
    takes that entry over.
    """
    if status not in STATUSES:
        raise _fail(f"--status must be {' or '.join(STATUSES)}, not {status!r}")
    chosen = _read_candidates(candidates)
    if sha:
        wanted = [prefix.lower() for prefix in sha]
        chosen = [c for c in chosen if any(c[1].sha.startswith(p) for p in wanted)]
        if not chosen:
            raise _fail(f"no candidate in {candidates} matches --sha {' '.join(sha)}")
    if top is not None:
        chosen = chosen[:top]
    added = claimed = refused = 0
    with _ledger(ledger, create=True) as opened:
        for rank, proposal in chosen:
            entry, verdict = opened.add(
                proposal,
                status=status,
                owner=owner,
                min_overlap=None if allow_overlap else min_overlap,
            )
            label = _label(rank, proposal)
            if entry is None:
                refused += 1
                typer.echo(f"refused  {label}  {verdict.describe(proposal.sha, proposal.repo)}")
            elif verdict.status is Status.DUPLICATE:
                claimed += 1
                typer.echo(
                    f"claimed  {label}  took over the proposed {entry.repo} {entry.sha[:10]} "
                    f"(first seen {entry.first_seen[:10]})"
                )
            else:
                added += 1
                note = "" if verdict.status is Status.NEW else f" ({verdict.status.value})"
                typer.echo(f"added    {label}  {entry.status}{note}")
    took = f", {claimed} claimed" if claimed else ""
    typer.echo(f"{added} added{took}, {refused} refused: {ledger}")
    if refused:
        raise typer.Exit(code=1)


@ledger_app.command(name="check")
def ledger_check(
    ledger: LedgerArgument,
    candidates: CandidatesArgument,
    min_overlap: MinOverlapOption = DEFAULT_MIN_OVERLAP,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one JSON object per candidate.")
    ] = False,
) -> None:
    """Check candidates against the ledger without writing (exit 1 if any is not new)."""
    chosen = _read_candidates(candidates)
    with _ledger(ledger, readonly=True) as opened:
        verdicts = check_all(opened, [proposal for _, proposal in chosen], min_overlap)
    for (rank, proposal), verdict in zip(chosen, verdicts, strict=True):
        if as_json:
            record = {"rank": rank, "repo": proposal.repo, "sha": proposal.sha}
            record.update(ledger_verdict_to_json(verdict))
            typer.echo(json.dumps(record, sort_keys=True, ensure_ascii=True))
        else:
            typer.echo(f"{_label(rank, proposal)}  {verdict.describe(proposal.sha, proposal.repo)}")
    if any(verdict.status is not Status.NEW for verdict in verdicts):
        raise typer.Exit(code=1)


@ledger_app.command(name="list")
def ledger_list(
    ledger: LedgerArgument,
    repo: Annotated[
        str | None, typer.Option("--repo", help="Only entries of this repository label.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print one JSON object per entry.")] = (
        False
    ),
) -> None:
    """List the recorded fixes, oldest first."""
    with _ledger(ledger, readonly=True) as opened:
        entries = opened.entries(repo)
        version = opened.schema_version
    if as_json:
        for entry in entries:
            typer.echo(json.dumps(entry_to_json(entry), sort_keys=True, ensure_ascii=True))
        return
    typer.echo(f"{ledger}: {len(entries)} entries (schema version {version})")
    if not entries:
        return
    typer.echo(
        f"{'first seen':<10}  {'status':<8}  {'owner':<10}  {'repo':<18}  {'sha':<10}  "
        f"{'hunks':>5}  {'fingerprint':<16}  subject"
    )
    for entry in entries:
        typer.echo(
            f"{entry.first_seen[:10]:<10}  {entry.status:<8}  {_ascii(entry.owner or '-'):<10}  "
            f"{_ascii(entry.repo):<18}  {entry.sha[:10]:<10}  {entry.hunk_count:>5}  "
            f"{entry.fingerprint:<16}  {_ascii(entry.subject)[:50]}"
        )


@ledger_app.command(name="watermarks")
def ledger_watermarks(
    ledger: LedgerArgument,
    as_json: Annotated[
        bool, typer.Option("--json", help="Print one JSON object per watermark.")
    ] = False,
) -> None:
    """List where the batch runs of each repository stopped."""
    with _ledger(ledger, readonly=True) as opened:
        marks = opened.watermarks()
    if as_json:
        for mark in marks:
            typer.echo(json.dumps(watermark_to_json(mark), sort_keys=True, ensure_ascii=True))
        return
    typer.echo(f"{ledger}: {len(marks)} watermark{'' if len(marks) == 1 else 's'}")
    if not marks:
        return
    typer.echo(
        f"{'repo':<24}  {'source':<13}  {'position':<20}  {'walked':>6}  {'runs':>4}  updated"
    )
    for mark in marks:
        position = mark.position or "-"
        if len(position) >= 40 and "-" not in position:
            position = position[:10]  # a commit sha; update times are shown in full
        typer.echo(
            f"{_ascii(mark.repo):<24}  {mark.source:<13}  {_ascii(position):<20}  "
            f"{mark.walked:>6}  {mark.runs:>4}  {mark.updated}"
        )


def _ascii(text: str) -> str:
    return text.encode("ascii", "replace").decode("ascii")


def main() -> None:
    """Console-script entry point."""
    app()
