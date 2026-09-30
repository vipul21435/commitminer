"""``commitminer batch``: several repositories into one ledger, resumed on the next run."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from commitminer import cli
from commitminer import ledger as ledger_module
from commitminer.cli import app
from commitminer.ledger import open_ledger
from gitrepo import GitRepo
from test_cli_ledger import BUILDER

ROOT = Path(__file__).resolve().parent.parent
TOMLI = ROOT / "examples" / "tomli"
runner = CliRunner()


@pytest.fixture(scope="module")
def repos(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, dict[str, str]]]:
    root = tmp_path_factory.mktemp("cli-batch") / "repos"
    return root, BUILDER.build(root)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_module, "utc_now", lambda: "2026-01-02T03:04:05+00:00")


def batch_file(tmp_path: Path, root: Path, extra: str = "") -> Path:
    path = tmp_path / "commitminer.toml"
    path.write_text(
        f"""
[batch]
ledger = "team.sqlite3"
{extra}

[[batch.repos]]
name = "demo/durations"
clone = "{root}/upstream"

[[batch.repos]]
name = "demo/durations-fork"
clone = "{root}/fork"

[[batch.repos]]
name = "hukkin/tomli"
github = "hukkin/tomli"
limit = 25
replay = "{TOMLI / "prs"}"
""",
        encoding="utf-8",
    )
    return path


def test_a_batch_records_every_repository_then_resumes(
    repos: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path
) -> None:
    root, shas = repos
    config = batch_file(tmp_path, root, 'out = "out/batch.jsonl"\nreport = "out/batch.md"')
    first = runner.invoke(app, ["batch", str(config), "--top", "2"])
    assert first.exit_code == 0, first.output
    lines = first.stdout.splitlines()
    assert lines[0] == f"batch {config}: 3 repositories, ledger {tmp_path / 'team.sqlite3'}"
    assert lines[1].startswith("demo/durations [clone]: first run, walked 4 commits: 3 candidates")
    assert lines[2].startswith("demo/durations-fork [clone]: first run, walked 5 commits: ")
    assert "1 new, 3 colliding with other repositories" in lines[2]
    assert lines[3].startswith("hukkin/tomli [pull-requests]: first run, walked 24 pull requests")
    assert "collisions with other repositories: 3 (3 same fix)" in lines
    assert "best new candidates across the batch (2 of 10):" in lines
    assert lines[-2] == f"wrote 3 runs and 13 candidates to {tmp_path / 'out' / 'batch.jsonl'}"
    assert lines[-1] == f"wrote the markdown report to {tmp_path / 'out' / 'batch.md'}"
    report = (tmp_path / "out" / "batch.md").read_text(encoding="utf-8")
    assert report.startswith("# CommitMiner batch report: ")
    assert report.splitlines()[0].endswith("commitminer.toml")
    records = [json.loads(line) for line in (tmp_path / "out" / "batch.jsonl").open()]
    assert [r["kind"] for r in records].count("run") == 3
    with open_ledger(tmp_path / "team.sqlite3") as ledger:
        assert len(ledger.entries()) == 10
        assert [m.source for m in ledger.watermarks()] == ["clone", "clone", "pull-requests"]
    # Next run: nothing new anywhere.
    again = runner.invoke(app, ["batch", str(config), "--out", str(tmp_path / "again.jsonl")])
    assert again.exit_code == 0, again.output
    text = again.stdout.splitlines()
    fix_b = shas["upstream"]["fix_b"][:10]
    assert text[1] == f"demo/durations [clone]: up to date at {fix_b}, walked 0 commits"
    assert text[3] == (
        "hukkin/tomli [pull-requests]: up to date at 2026-04-14T16:51:36Z, walked 0 pull requests"
    )
    assert "collisions with other repositories: none" in text
    assert "no new candidates" in text
    assert text[-2] == f"wrote 3 runs and 0 candidates to {tmp_path / 'again.jsonl'}"


def test_new_commits_in_a_clone_are_the_only_ones_walked(tmp_path: Path) -> None:
    repo = GitRepo(tmp_path / "live")
    files = {"src/pkg/mod.py": "x = 1\n", "tests/test_mod.py": "def test_x():\n    assert 1\n"}
    repo.commit("Fix x (fixes #1)", files)
    config = tmp_path / "batch.toml"
    config.write_text(f'[[batch.repos]]\nname = "live"\nclone = "{repo.root}"\n')
    ledger = tmp_path / ".commitminer" / "ledger.sqlite3"
    assert runner.invoke(app, ["batch", str(config)]).exit_code == 0
    assert ledger.exists()
    watermark = repo.head()
    new = repo.commit(
        "Fix y (fixes #2)",
        {"src/pkg/mod.py": "x = 2\n", "tests/test_mod.py": "def test_x():\n    assert 2\n"},
    )
    second = runner.invoke(app, ["batch", str(config), "--top", "1"])
    assert second.exit_code == 0, second.output
    assert second.stdout.splitlines()[1] == (
        f"live [clone]: resumed from {watermark[:10]}, walked 1 commit: 1 candidate, 0 rejected; "
        f"1 new; watermark {new[:10]}"
    )


def test_only_full_and_dry_run(
    repos: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path
) -> None:
    root, _ = repos
    config = batch_file(tmp_path, root)
    ledger = tmp_path / "team.sqlite3"
    # A dry run of a ledger that does not exist yet creates nothing.
    dry = runner.invoke(app, ["batch", str(config), "--dry-run", "--top", "0"])
    assert dry.exit_code == 0, dry.output
    assert dry.stdout.splitlines()[0].endswith("(dry run: the ledger is not changed)")
    assert "collisions with other repositories: 3 (3 same fix)" in dry.stdout
    assert "best new candidates" not in dry.stdout
    assert not ledger.exists()
    only = runner.invoke(app, ["batch", str(config), "--only", "demo/durations", "--top", "0"])
    assert only.exit_code == 0, only.output
    assert len(only.stdout.splitlines()) == 3
    # A dry run of an existing ledger leaves it as it was.
    out = tmp_path / "dry.jsonl"
    dry2 = runner.invoke(app, ["batch", str(config), "--dry-run", "--out", str(out)])
    assert dry2.exit_code == 0, dry2.output
    assert json.loads(out.read_text().splitlines()[0])["dry_run"] is True
    assert json.loads(out.read_text().splitlines()[0])["ledger"] == str(ledger)
    with open_ledger(ledger) as opened:
        assert len(opened.entries()) == 3
        assert len(opened.watermarks()) == 1
    full = runner.invoke(
        app, ["batch", str(config), "--only", "demo/durations", "--full", "--top", "0"]
    )
    assert full.exit_code == 0, full.output
    assert "full walk, walked 4 commits: 3 candidates, 1 rejected; 3 already recorded" in (
        full.stdout
    )


def test_batch_errors_exit_2(repos: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path) -> None:
    root, _ = repos
    missing = runner.invoke(app, ["batch", str(tmp_path / "none.toml")])
    assert missing.exit_code == 2
    assert "none.toml: cannot read" in missing.stderr
    config = batch_file(tmp_path, root)
    unknown = runner.invoke(app, ["batch", str(config), "--only", "nope"])
    assert unknown.exit_code == 2
    assert "no repository named 'nope'" in unknown.stderr
    directory = runner.invoke(app, ["batch", str(config), "--ledger", str(tmp_path)])
    assert directory.exit_code == 2
    assert "is a directory" in directory.stderr
    blocked = tmp_path / "file"
    blocked.write_text("")
    no_dir = runner.invoke(app, ["batch", str(config), "--ledger", str(blocked / "l.sqlite3")])
    assert no_dir.exit_code == 2
    assert "cannot create the ledger's directory" in no_dir.stderr
    unwritable = runner.invoke(
        app, ["batch", str(config), "--out", str(blocked / "x.jsonl"), "--ledger", "l.sqlite3"]
    )
    assert unwritable.exit_code == 2
    assert "cannot write" in unwritable.stderr
    broken = tmp_path / "broken.toml"
    broken.write_text(
        '[[batch.repos]]\nname = "gone"\nhistory = "missing.jsonl.gz"\n'
        f'[[batch.repos]]\nname = "up"\nclone = "{root}/upstream"\n'
    )
    failed = runner.invoke(app, ["batch", str(broken), "--ledger", str(tmp_path / "b.sqlite3")])
    assert failed.exit_code == 2
    assert failed.stdout.splitlines()[1].startswith("gone [history]: failed: ")
    assert failed.stdout.splitlines()[2].startswith("up [clone]: first run")
    assert "1 of 2 repositories failed: gone [history]" in failed.stderr


def test_pull_requests_go_to_the_network_without_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(404, json={"message": "Not Found"})

    monkeypatch.setattr(cli, "_network_transport", lambda: httpx.MockTransport(answer))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg"))
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    config = tmp_path / "batch.toml"
    config.write_text(
        '[[batch.repos]]\nname = "o/r"\ngithub = "o/r"\n'
        '[[batch.repos]]\nname = "o/cached"\ngithub = "hukkin/tomli"\nlimit = 25\n'
        f'replay = "{TOMLI / "prs"}"\ncache_dir = "cache"\n'
    )
    result = runner.invoke(app, ["batch", str(config), "--top", "0"])
    assert result.exit_code == 2
    assert seen == [
        "https://api.github.com/repos/o/r/pulls?state=closed&sort=updated&direction=desc&per_page=100"
    ]
    assert "o/r [pull-requests]: failed: o/r: not found" in result.stdout
    assert "o/cached [pull-requests]: first run, walked 24 pull requests" in result.stdout
    assert (tmp_path / "cache").is_dir()
    assert list((tmp_path / "cache").iterdir())
