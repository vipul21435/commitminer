"""Command-line interface for CommitMiner."""

from __future__ import annotations

import typer

from commitminer import __version__

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


def main() -> None:
    """Console-script entry point."""
    app()
