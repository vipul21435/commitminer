"""Command-line interface for CommitMiner."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from commitminer import __version__
from commitminer.export import render_explanation, render_summary, render_table, write_jsonl
from commitminer.gitlog import GitError, head_sha, walk
from commitminer.history import HistoryError, read_history, write_history
from commitminer.models import Commit
from commitminer.scoring import Settings, mine

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


def _fail(message: str, code: int = 1) -> typer.Exit:
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
    max_lines: Annotated[
        int, typer.Option("--max-lines", min=1, help="Largest source+test diff to accept.")
    ] = 400,
    test_lines_cap: Annotated[
        int,
        typer.Option("--test-lines-cap", min=1, help="Added test lines for a full test score."),
    ] = 40,
    top: Annotated[int, typer.Option("--top", min=0, help="Rows to print in the table.")] = 10,
    explain: Annotated[
        int, typer.Option("--explain", min=0, help="Print the score breakdown of the best N.")
    ] = 1,
    out: Annotated[
        Path | None, typer.Option("--out", help="Write all candidates to this JSONL file.")
    ] = None,
    repo_name: RepoNameOption = None,
) -> None:
    """Filter and rank the commits of a clone or a recorded history."""
    if (repo is None) == (history is None):
        # Plain text on purpose: Typer's rich usage panel re-wraps messages by terminal width.
        raise _fail("give either a REPO path or --history FILE, not both or neither", code=2)
    commits: list[Commit]
    try:
        if history is not None:
            header, commits = read_history(history)
            label = repo_name or header.repo
        else:
            assert repo is not None
            commits = walk(repo, rev, max_count)
            label = repo_name or repo.resolve().name
    except (GitError, HistoryError, OSError) as exc:
        raise _fail(str(exc)) from exc
    if history is not None and max_count is not None:
        commits = commits[:max_count]
    result = mine(commits, Settings(max_lines=max_lines, test_lines_cap=test_lines_cap))
    typer.echo(render_summary(result, label))
    if top and result.candidates:
        typer.echo("")
        typer.echo(render_table(result, top))
    for rank, candidate in enumerate(result.candidates[:explain], start=1):
        typer.echo("")
        typer.echo(render_explanation(candidate, rank))
    if out is not None:
        count = write_jsonl(out, result, label)
        typer.echo("")
        typer.echo(f"wrote {count} candidates to {out}")


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
) -> None:
    """Walk a clone once and save its history for offline replay with mine --history."""
    try:
        head = head_sha(repo, rev)
        commits = walk(repo, head, max_count)
    except GitError as exc:
        raise _fail(str(exc)) from exc
    header = write_history(out, commits, repo=repo_name or repo.resolve().name, url=url, head=head)
    typer.echo(f"recorded {header.commits} commits of {header.repo} at {head[:12]} to {out}")


def main() -> None:
    """Console-script entry point."""
    app()
