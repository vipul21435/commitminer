from __future__ import annotations

import runpy
from pathlib import Path

import pytest
from typer.testing import CliRunner

from commitminer import __version__
from commitminer.cli import app
from exportfile import read_export
from gitrepo import GitRepo, lines

runner = CliRunner()


def test_version_command_prints_version() -> None:
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == f"commitminer {__version__}"


def test_no_arguments_shows_help() -> None:
    result = runner.invoke(app, [])
    # Typer exits with 2 when no_args_is_help prints the usage.
    assert result.exit_code == 2
    assert "mine" in result.stdout
    assert "record" in result.stdout


def test_python_dash_m_entry_point(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("sys.argv", ["commitminer", "version"])
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("commitminer", run_name="__main__")
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"commitminer {__version__}"


@pytest.fixture
def small_repo(git_repo: GitRepo) -> GitRepo:
    """Four commits: a setup, a good fix, a docs change and an oversized feature."""
    git_repo.commit("Initial layout", {"src/pkg/core.py": lines(20), "README.md": "hi\n"})
    git_repo.commit(
        "Fix off-by-one in core (fixes #4)",
        {"src/pkg/core.py": lines(20) + "fixed\n", "tests/test_core.py": lines(8, "assert")},
    )
    git_repo.commit("Document usage", {"docs/usage.md": lines(5)})
    git_repo.commit(
        "Add a big feature",
        {"src/pkg/big.py": lines(500), "tests/test_big.py": lines(10, "assert")},
    )
    return git_repo


def test_mine_a_clone_prints_summary_table_and_breakdown(
    small_repo: GitRepo, tmp_path: Path
) -> None:
    out = tmp_path / "out" / "candidates.jsonl"
    result = runner.invoke(app, ["mine", str(small_repo.root), "--out", str(out)])
    assert result.exit_code == 0, result.output
    text = result.stdout
    assert text.startswith(
        "repo: walked 4 commits, 1 candidates (easy 1), 3 rejected "
        "(docs-only 1, no-test 1, oversize 1)"
    )
    assert "Fix off-by-one in core (fixes #4)" in text
    assert "linked_reference" in text
    assert f"wrote 1 candidates to {out}" in text
    run, (record,) = read_export(out)
    assert (run["repo"], run["source"], run["walked"], run["exported"]) == ("repo", "clone", 4, 1)
    assert run["url"] is None  # the synthetic clone has no origin remote
    assert record["repo"] == "repo"
    assert record["repo_url"] is None
    small_repo.git("remote", "add", "origin", "https://example.invalid/demo/repo.git")
    again = runner.invoke(app, ["mine", str(small_repo.root), "--out", str(out), "--top", "0"])
    assert again.exit_code == 0, again.output
    run, (record,) = read_export(out)
    assert run["url"] == record["repo_url"] == "https://example.invalid/demo/repo.git"
    given = runner.invoke(
        app, ["mine", str(small_repo.root), "--out", str(out), "--url", "https://example.invalid/r"]
    )
    assert given.exit_code == 0, given.output
    assert read_export(out)[0]["url"] == "https://example.invalid/r"
    assert record["subject"] == "Fix off-by-one in core (fixes #4)"
    assert record["test_files"] == ["tests/test_core.py"]


SECRET = "ghp_EXAMPLEsecretTOKEN0123"


def test_credentials_in_the_origin_remote_are_not_exported(
    small_repo: GitRepo, tmp_path: Path
) -> None:
    # CI job clones and private-repository clones keep a token in remote.origin.url;
    # exports and reports are handed on, so it must not reach them.
    remote = f"https://build-bot:{SECRET}@github.com/example/calc.git"
    small_repo.git("remote", "add", "origin", remote)
    out = tmp_path / "c.jsonl"
    mined = runner.invoke(app, ["mine", str(small_repo.root), "--out", str(out), "--top", "0"])
    assert mined.exit_code == 0, mined.output
    run, (record,) = read_export(out)
    assert run["url"] == record["repo_url"] == "https://github.com/example/calc.git"
    for name in ("r.md", "r.html"):
        report = tmp_path / name
        rendered = runner.invoke(app, ["report", str(out), "--out", str(report)])
        assert rendered.exit_code == 0, rendered.output
        text = report.read_text()
        assert SECRET not in text
        assert "https://github.com/example/calc/commit/" in text
    assert SECRET not in out.read_text()
    given = runner.invoke(
        app, ["mine", str(small_repo.root), "--out", str(out), "--url", f"https://{SECRET}@h/r"]
    )
    assert given.exit_code == 0, given.output
    assert SECRET not in out.read_text()
    recording = tmp_path / "h.jsonl"
    args = ["record", str(small_repo.root), "--out", str(recording), "--url", remote]
    assert runner.invoke(app, args).exit_code == 0
    assert SECRET not in recording.read_text()


def test_fixes_to_tests_with_wrapped_signatures_name_them(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    def calc_tests(first: int, second: int) -> str:
        return (
            "from pkg.calc import add\n\n\n"
            "def test_add_with_fixtures(\n"
            "    tmp_path, monkeypatch\n"
            ") -> None:\n"
            "    result = add(1, 2)\n"
            f"    assert result == {first}\n\n\n"
            "class TestCalc:\n"
            "    def test_add_again(\n"
            "        self, tmp_path, monkeypatch\n"
            "    ) -> None:\n"
            f"        assert add(2, 2) == {second}\n"
        )

    git_repo.commit(
        "Add add",
        {
            "src/pkg/calc.py": "def add(a, b):\n    return a - b\n",
            "tests/test_calc.py": calc_tests(-1, 0),
        },
    )
    git_repo.commit(
        "Fix add",
        {
            "src/pkg/calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": calc_tests(3, 4),
        },
    )
    out = tmp_path / "c.jsonl"
    result = runner.invoke(app, ["mine", str(git_repo.root), "--out", str(out), "--top", "0"])
    assert result.exit_code == 0, result.output
    (record,) = [r for r in read_export(out)[1] if r["subject"] == "Fix add"]
    assert record["fail_to_pass"] == [
        "tests/test_calc.py::TestCalc::test_add_again",
        "tests/test_calc.py::test_add_with_fixtures",
    ]


def test_without_contents_a_method_whose_class_is_not_in_the_diff_is_not_named(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    header = "from pkg.calc import add\n\n\nclass TestCalc:\n"
    old_test = "    def test_one(self):\n        assert add(0, 1) == 1\n"
    new_test = "\n    def test_two(self):\n        assert add(1, 1) == 2\n"
    git_repo.commit(
        "Add add",
        {
            "src/pkg/calc.py": "def add(a, b):\n    return b\n",
            "tests/test_calc.py": header + old_test,
        },
    )
    git_repo.commit(
        "Fix add",
        {
            "src/pkg/calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": header + old_test + new_test,
        },
    )
    out = tmp_path / "c.jsonl"
    ids = {}
    for flag in ("--content", "--no-content"):
        args = ["mine", str(git_repo.root), flag, "--out", str(out), "--top", "0"]
        assert runner.invoke(app, args).exit_code == 0
        (record,) = [r for r in read_export(out)[1] if r["subject"] == "Fix add"]
        ids[flag] = record["fail_to_pass"]
    assert ids == {"--content": ["tests/test_calc.py::TestCalc::test_two"], "--no-content": []}


def test_record_then_replay_matches_mining_the_clone(small_repo: GitRepo, tmp_path: Path) -> None:
    recording = tmp_path / "history.jsonl.gz"
    recorded = runner.invoke(
        app,
        ["record", str(small_repo.root), "--out", str(recording), "--repo-name", "demo/small"],
    )
    assert recorded.exit_code == 0, recorded.output
    assert recorded.stdout.startswith("recorded 4 commits of demo/small at ")
    live = runner.invoke(app, ["mine", str(small_repo.root), "--repo-name", "demo/small"])
    replay = runner.invoke(app, ["mine", "--history", str(recording)])
    assert replay.exit_code == 0, replay.output
    assert replay.stdout == live.stdout


def test_max_count_applies_to_clones_and_recordings(small_repo: GitRepo, tmp_path: Path) -> None:
    recording = tmp_path / "h.jsonl"
    runner.invoke(app, ["record", str(small_repo.root), "--out", str(recording)])
    for source in ([str(small_repo.root)], ["--history", str(recording)]):
        result = runner.invoke(app, ["mine", *source, "--max-count", "2", "--top", "0"])
        assert result.exit_code == 0, result.output
        assert "walked 2 commits, 0 candidates" in result.stdout
        assert "rank" not in result.stdout


def test_top_and_explain_can_be_turned_off(small_repo: GitRepo) -> None:
    result = runner.invoke(app, ["mine", str(small_repo.root), "--top", "0", "--explain", "0"])
    assert result.exit_code == 0
    assert result.stdout.count("\n") == 1


def test_size_limit_option_changes_the_filter(small_repo: GitRepo) -> None:
    result = runner.invoke(app, ["mine", str(small_repo.root), "--max-lines", "1000"])
    assert "2 candidates" in result.stdout


@pytest.mark.parametrize("args", [["mine"], ["mine", ".", "--history", "h.jsonl"]])
def test_mine_needs_exactly_one_source(args: list[str]) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == 2
    assert "either a REPO path or --history" in result.output


def test_mine_reports_git_and_history_errors(tmp_path: Path) -> None:
    not_a_repo = runner.invoke(app, ["mine", str(tmp_path)])
    assert not_a_repo.exit_code == 2
    assert "error: git exited with" in not_a_repo.output
    bad = tmp_path / "bad.jsonl"
    bad.write_text("{}\n")
    bad_history = runner.invoke(app, ["mine", "--history", str(bad)])
    assert bad_history.exit_code == 2
    assert "error: line 1: not a commitminer-history file" in bad_history.output
    missing = runner.invoke(app, ["mine", "--history", str(tmp_path / "missing.jsonl")])
    assert missing.exit_code == 2
    assert "No such file" in missing.output


def test_record_reports_bad_revisions(small_repo: GitRepo, tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["record", str(small_repo.root), "--rev", "no-such-branch", "--out", "x.jsonl"]
    )
    assert result.exit_code == 2
    assert "error: git exited with" in result.output
    assert not (tmp_path / "x.jsonl").exists()


def test_mine_reads_limits_from_the_config_and_the_command_line_wins(small_repo: GitRepo) -> None:
    (small_repo.root / "commitminer.toml").write_text("[filter]\nmax_lines = 1000\n")
    result = runner.invoke(app, ["mine", str(small_repo.root), "--explain", "0"])
    assert result.exit_code == 0, result.output
    lines_out = result.stdout.splitlines()
    assert lines_out[0].endswith("0 built-in rules disabled, 1 scoring setting")
    assert "2 candidates" in lines_out[1]
    flag = runner.invoke(
        app,
        ["mine", str(small_repo.root), "--max-lines", "400", "--max-source-files", "1"],
    )
    assert "1 candidates" in flag.stdout.splitlines()[1]


def test_a_bad_glob_in_the_mined_clone_config_stops_before_walking(small_repo: GitRepo) -> None:
    # A regex compiled lazily used to crash with a traceback after the whole walk.
    (small_repo.root / "commitminer.toml").write_text(
        '[[classify.rules]]\nid = "fx"\ncategory = "test"\npaths = ["**/**/**/**/x", "a//b"]\n'
        'rationale = "r"\n'
    )
    result = runner.invoke(app, ["mine", str(small_repo.root)])
    assert result.exit_code == 2
    assert "Traceback" not in result.output
    assert "rule fx: 'a//b': empty path segment" in result.stderr
    assert result.stdout == ""
