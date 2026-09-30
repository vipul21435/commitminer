"""``commitminer prs`` on tomli's recorded pull requests (offline)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from commitminer import cli
from commitminer.cli import app
from commitminer.fixtures import ReplayTransport
from commitminer.history import read_history
from commitminer.scoring import mine
from exportfile import read_export

runner = CliRunner()
EXAMPLE = Path(__file__).resolve().parent.parent / "examples" / "tomli"
PRS = EXAMPLE / "prs"
REPLAY = ["prs", "hukkin/tomli", "--limit", "25", "--replay", str(PRS)]


def test_prs_ranks_the_recorded_pull_requests(tmp_path: Path) -> None:
    out = tmp_path / "prs.jsonl"
    result = runner.invoke(app, [*REPLAY, "--top", "3", "--explain", "1", "--out", str(out)])
    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[0].startswith("github: 52 requests (0 answered 304 from the cache), 0 retries")
    assert lines[0].endswith(f"(replayed from {PRS})")
    assert lines[1] == (
        "hukkin/tomli: read 24 merged pull requests (6 closed without merging passed over; "
        "skipped #278: more than 300 changed files)"
    )
    assert lines[2] == (
        "hukkin/tomli: walked 24 pull requests, 6 candidates (easy 4, medium 2), 18 rejected "
        "(docs-only 1, no-source 11, source-cosmetic 1, no-test 5)"
    )
    assert lines[4].split()[3] == "pull"
    assert [line.split()[4] for line in lines[5:8]] == ["#200", "#295", "#286"]
    assert lines[9].startswith("#1 #200 score 6.55, difficulty 2.10 (medium)")
    run, records = read_export(out)
    assert len(records) == 6
    assert {r["schema_version"] for r in records} == {6}
    assert (run["source"], run["unit"], run["walked"]) == ("pull-requests", "pull requests", 24)
    assert run["url"] == records[0]["repo_url"] == "https://github.com/hukkin/tomli"
    assert run["rejections"][0]["pull_request"] is not None
    first = records[0]["pull_request"]
    assert (first["number"], first["base_ref"]) == (200, "master")
    assert first["url"] == "https://github.com/hukkin/tomli/pull/200"
    assert records[0]["base"] == first["base_sha"]


def test_replay_ignores_an_enterprise_api_url_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # GitHub Enterprise runners set this; the fixtures were recorded from api.github.com.
    monkeypatch.setenv("GITHUB_API_URL", "https://ghe.example.invalid/api/v3")
    out = tmp_path / "prs.jsonl"
    result = runner.invoke(app, [*REPLAY, "--top", "0", "--explain", "0", "--out", str(out)])
    assert result.exit_code == 0, result.output
    assert "hukkin/tomli: read 24 merged pull requests" in result.stdout
    assert read_export(out)[0]["url"] == "https://github.com/hukkin/tomli"


def test_pull_requests_score_like_the_squashed_commits_they_became(tmp_path: Path) -> None:
    out = tmp_path / "prs.jsonl"
    assert runner.invoke(app, [*REPLAY, "--top", "0", "--out", str(out)]).exit_code == 0
    _, commits = read_history(EXAMPLE / "history.jsonl.gz")
    mined = {c.commit.sha: c for c in mine(commits).candidates}
    for record in read_export(out)[1]:
        commit = mined[record["sha"]]
        assert record["fingerprint"]["patch"] == commit.fingerprint.patch  # type: ignore[union-attr]
        assert record["score"] == commit.score


def test_the_etag_cache_answers_a_second_run(tmp_path: Path) -> None:
    cache = ["--cache-dir", str(tmp_path / "cache"), "--top", "0", "--explain", "0"]
    first = runner.invoke(app, [*REPLAY, *cache])
    second = runner.invoke(app, [*REPLAY, *cache])
    assert first.exit_code == second.exit_code == 0
    assert "(0 answered 304 from the cache)" in first.stdout
    assert "52 requests (52 answered 304 from the cache)" in second.stdout
    assert first.stdout.splitlines()[1:] == second.stdout.splitlines()[1:]


def test_pull_requests_are_checked_against_the_ledger(tmp_path: Path) -> None:
    commits, ledger = tmp_path / "commits.jsonl", tmp_path / "ledger.sqlite3"
    mined = runner.invoke(
        app, ["mine", "--history", str(EXAMPLE / "history.jsonl.gz"), "--out", str(commits)]
    )
    assert mined.exit_code == 0
    assert runner.invoke(app, ["ledger", "add", str(ledger), str(commits)]).exit_code == 0
    result = runner.invoke(app, [*REPLAY, "--ledger", str(ledger), "--new-only", "--explain", "0"])
    assert result.exit_code == 0, result.output
    assert f"ledger {ledger}: 6 duplicate" in result.stdout
    assert "  #1 #200 duplicate: already in the ledger" in result.stdout
    assert "showing the 0 new candidates (--new-only)" in result.stdout


def test_record_writes_the_same_fixtures_it_was_served(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_network_transport", lambda: ReplayTransport(PRS))
    recorded = tmp_path / "recorded"
    result = runner.invoke(
        app, ["prs", "hukkin/tomli", "--limit", "25", "--record", str(recorded), "--top", "0"]
    )
    assert result.exit_code == 0, result.output
    assert f"recorded 52 responses to {recorded}" in result.stdout
    originals = sorted(PRS.glob("*.json"))
    assert [p.name for p in originals] == sorted(p.name for p in recorded.iterdir())
    for path in originals:
        assert (recorded / path.name).read_text() == path.read_text()


def test_live_runs_use_the_default_cache_and_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str | None] = []
    replay = ReplayTransport(PRS)

    def network(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return replay.handle_request(request)

    monkeypatch.setattr(cli, "_network_transport", lambda: httpx.MockTransport(network))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("GITHUB_API_URL", "https://api.github.com")
    result = runner.invoke(app, ["prs", "hukkin/tomli", "--limit", "2", "--top", "0"])
    assert result.exit_code == 0, result.output
    assert set(seen) == {"Bearer t0ken"}
    assert len(list((tmp_path / "commitminer" / "github").glob("*.json"))) == len(seen) == 5
    uncached = runner.invoke(app, ["prs", "hukkin/tomli", "--limit", "2", "--no-cache"])
    assert "(0 answered 304 from the cache)" in uncached.stdout


def test_web_url_for_github_and_an_enterprise_host() -> None:
    assert cli.web_url("https://api.github.com", "o/r") == "https://github.com/o/r"
    assert cli.web_url("https://api.github.com/", "o/r") == "https://github.com/o/r"
    assert cli.web_url("https://ghe.example.invalid/api/v3", "o/r") == (
        "https://ghe.example.invalid/o/r"
    )
    assert (
        cli.web_url("https://proxy.example.invalid/", "o/r") == "https://proxy.example.invalid/o/r"
    )


@pytest.mark.parametrize(
    ("args", "message", "code"),
    [
        (["--record", "a", "--replay", "b"], "give --record or --replay, not both", 2),
        (["--record", "a", "--cache-dir", "b"], "drop --cache-dir", 2),
        (["--new-only"], "--new-only needs --ledger", 2),
        (["--replay", "no-such-dir"], "no-such-dir: no such fixture directory", 2),
    ],
)
def test_prs_argument_errors(args: list[str], message: str, code: int) -> None:
    result = runner.invoke(app, ["prs", "hukkin/tomli", *args])
    assert result.exit_code == code
    assert message in result.stderr


def test_prs_reports_api_errors(tmp_path: Path) -> None:
    result = runner.invoke(app, ["prs", "not-a-repo", "--replay", str(PRS)])
    assert result.exit_code == 2
    assert "'not-a-repo' is not OWNER/REPO" in result.stderr
    missing = runner.invoke(app, ["prs", "hukkin/other", "--replay", str(PRS)])
    assert missing.exit_code == 2
    assert "no recorded response for GET /repos/hukkin/other/pulls" in missing.stderr


def test_prs_uses_a_config_file(tmp_path: Path) -> None:
    config = tmp_path / "commitminer.toml"
    config.write_text("[score.weights]\nlinked_reference = 0\n")
    result = runner.invoke(app, [*REPLAY, "--config", str(config), "--top", "1"])
    assert result.exit_code == 0, result.output
    assert f"config: {config}: 0 custom rules" in result.stdout
    assert "linked_reference   0.500   0.00   0.000" in result.stdout


def test_the_network_transport_is_plain_httpx() -> None:
    transport = cli._network_transport()
    assert isinstance(transport, httpx.HTTPTransport)
    transport.close()


def test_the_cli_copies_of_the_defaults_match() -> None:
    from commitminer import github, pulls

    assert cli.API_URL == github.API_URL
    assert cli.DEFAULT_MAX_FILES == pulls.DEFAULT_MAX_FILES
