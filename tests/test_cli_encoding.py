"""History text that is not UTF-8 reaches exports, reports, the ledger and batches intact."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commitminer.cli import app
from exportfile import read_export
from gitrepo import GitRepo

runner = CliRunner()
LATIN1_SUBJECT = b"Corrige la fonction f (r\xe9gression)"
EXPORTED = "Corrige la fonction f (r\udce9gression)"
SHOWN = "Corrige la fonction f (r�gression)"


@pytest.fixture
def latin1_repo(git_repo: GitRepo) -> GitRepo:
    """A fix whose commit message is Latin-1 without an encoding header, as old gits wrote."""
    git_repo.commit("Add f", {"src/pkg/calc.py": "def f(a, b):\n    return a - b\n"})
    git_repo.commit(
        "placeholder",
        {
            "src/pkg/calc.py": "def f(a, b):\n    return a + b\n",
            "tests/test_calc.py": "def test_f():\n    assert f(1, 2) == 3\n",
        },
    )
    raw = subprocess.run(
        ["git", "-C", str(git_repo.root), "cat-file", "commit", "HEAD"],
        capture_output=True,
        check=True,
    ).stdout
    header = raw.split(b"\n\n", 1)[0]
    rewritten = subprocess.run(
        ["git", "-C", str(git_repo.root), "hash-object", "-t", "commit", "-w", "--stdin"],
        input=header + b"\n\n" + LATIN1_SUBJECT + b"\n",
        capture_output=True,
        check=True,
    ).stdout.strip()
    git_repo.git("update-ref", "refs/heads/main", rewritten.decode())
    return git_repo


def test_reports_of_non_utf8_subjects_are_written(latin1_repo: GitRepo, tmp_path: Path) -> None:
    export = tmp_path / "c.jsonl"
    mined = runner.invoke(app, ["mine", str(latin1_repo.root), "--out", str(export), "--top", "0"])
    assert mined.exit_code == 0, mined.output
    assert read_export(export)[1][0]["subject"] == EXPORTED
    for name in ("r.md", "r.html"):
        report = tmp_path / name
        report.write_text("an earlier report\n")
        result = runner.invoke(app, ["report", str(export), "--out", str(report)])
        assert result.exit_code == 0, result.output
        assert SHOWN in report.read_text(encoding="utf-8")


def test_the_ledger_stores_non_utf8_subjects(latin1_repo: GitRepo, tmp_path: Path) -> None:
    export, ledger = tmp_path / "c.jsonl", tmp_path / "l.sqlite3"
    mined = runner.invoke(app, ["mine", str(latin1_repo.root), "--out", str(export)])
    assert mined.exit_code == 0, mined.output
    added = runner.invoke(app, ["ledger", "add", str(ledger), str(export), "--owner", "x"])
    assert added.exit_code == 0, added.output
    listed = runner.invoke(app, ["ledger", "list", str(ledger), "--json"])
    assert listed.exit_code == 0, listed.output
    assert SHOWN.replace("�", "\\ufffd") in listed.stdout
    checked = runner.invoke(app, ["mine", str(latin1_repo.root), "--ledger", str(ledger)])
    assert checked.exit_code == 0, checked.output
    assert "1 duplicate" in checked.stdout


def test_a_batch_records_non_utf8_subjects(latin1_repo: GitRepo, tmp_path: Path) -> None:
    config = tmp_path / "batch.toml"
    config.write_text(f'[[batch.repos]]\nname = "example/calc"\nclone = "{latin1_repo.root}"\n')
    report = tmp_path / "batch.html"
    args = ["--ledger", str(tmp_path / "l.sqlite3"), "--report", str(report)]
    result = runner.invoke(app, ["batch", str(config), *args])
    assert result.exit_code == 0, result.output
    assert SHOWN in report.read_text(encoding="utf-8")
