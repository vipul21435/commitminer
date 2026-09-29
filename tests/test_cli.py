import runpy

import pytest
from typer.testing import CliRunner

from commitminer import __version__
from commitminer.cli import app

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"commitminer {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    # Typer exits with 2 when no_args_is_help prints the usage.
    assert result.exit_code == 2
    assert "version" in result.stdout


def test_python_dash_m_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["commitminer", "version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("commitminer", run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"commitminer {__version__}"
