"""``mine --ledger`` and ``ledger add|check|list`` on the demo's upstream and fork repositories.

The repositories come from ``examples/ledger/build_repos.py`` (the script behind
``make demo-ledger``): an upstream with a release branch that cherry-picks two
fixes (one cleanly, one with a resolved conflict), and an independent fork that
re-indented the code and later moved it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from functools import partial
from pathlib import Path
from types import ModuleType

import pytest
from typer.testing import CliRunner

from commitminer import cli
from commitminer import ledger as ledger_module
from commitminer.cli import app
from gitrepo import GitRepo

ROOT = Path(__file__).resolve().parent.parent
runner = CliRunner()


def _builder() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_repos", ROOT / "examples" / "ledger" / "build_repos.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BUILDER = _builder()


@pytest.fixture(scope="module")
def demo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, dict[str, dict[str, str]]]:
    root = tmp_path_factory.mktemp("ledger-demo") / "repos"
    return root, BUILDER.build(root)


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ledger_module, "utc_now", lambda: "2026-01-02T03:04:05+00:00")


def mine(*args: str) -> str:
    result = runner.invoke(app, ["mine", *args, "--explain", "0"])
    assert result.exit_code == 0, result.output
    return result.stdout


@pytest.fixture
def claimed(
    demo: tuple[Path, dict[str, dict[str, str]]], tmp_path: Path
) -> tuple[Path, Path, dict[str, dict[str, str]]]:
    """A ledger holding upstream main's three candidates, claimed by alice."""
    root, shas = demo
    exported = tmp_path / "upstream.jsonl"
    mine(str(root / "upstream"), "--repo-name", "demo/durations", "--out", str(exported))
    ledger = tmp_path / "team.sqlite3"
    added = runner.invoke(app, ["ledger", "add", str(ledger), str(exported), "--owner", "alice"])
    assert added.exit_code == 0, added.output
    assert added.stdout.splitlines() == [
        f"added    #1 demo/durations {shas['upstream']['fix_a'][:10]}  claimed",
        f"added    #2 demo/durations {shas['upstream']['fix_b'][:10]}  claimed",
        f"added    #3 demo/durations {shas['upstream']['base'][:10]}  claimed",
        f"3 added, 0 refused: {ledger}",
    ]
    return ledger, exported, shas


def test_the_release_branch_cherry_picks_are_marked(
    demo: tuple[Path, dict[str, dict[str, str]]],
    claimed: tuple[Path, Path, dict[str, dict[str, str]]],
) -> None:
    root, _ = demo
    ledger, _, shas = claimed
    up = shas["upstream"]
    text = mine(
        str(root / "upstream"), "--rev", "release", "--repo-name", "demo/durations",
        "--ledger", str(ledger),
    )  # fmt: skip
    lines = text.splitlines()
    assert lines[1] == f"ledger {ledger}: 1 new, 2 duplicate, 1 overlap"
    assert lines[2] == (
        f"  #1 {up['pick_a'][:10]} duplicate: same fix as demo/durations {up['fix_a'][:10]} "
        "(claimed by alice on 2026-01-02)"
    )
    assert lines[3] == (
        f"  #3 {up['pick_b'][:10]} overlap: 2 of 3 hunks shared with demo/durations "
        f"{up['fix_b'][:10]} (claimed by alice on 2026-01-02)"
    )
    assert lines[4] == (
        f"  #4 {up['base'][:10]} duplicate: already in the ledger (claimed by alice on 2026-01-02)"
    )
    table = "\n".join(lines[6:])
    assert "  ledger   subject" in table
    assert "  new      Accept upper-case units (fixes #11)" in table
    assert "  overlap  Accept days and weeks (fixes #9)" in table


def test_the_fork_copies_are_duplicates_and_its_own_fix_is_new(
    demo: tuple[Path, dict[str, dict[str, str]]],
    claimed: tuple[Path, Path, dict[str, dict[str, str]]],
    tmp_path: Path,
) -> None:
    root, _ = demo
    ledger, _, shas = claimed
    fork, up = shas["fork"], shas["upstream"]
    out = tmp_path / "fork.jsonl"
    text = mine(
        str(root / "fork"), "--repo-name", "demo/fork", "--ledger", str(ledger), "--out", str(out)
    )
    assert "walked 5 commits, 4 candidates (easy 4), 1 rejected (source-unchanged 1)" in text
    assert f"ledger {ledger}: 1 new, 3 duplicate" in text
    # Re-indented (fix A), re-indented and moved (fix B), and the imported code itself.
    for mine_key, up_key in (("fix_a", "fix_a"), ("fix_b", "fix_b"), ("base", "base")):
        assert f"{fork[mine_key][:10]} duplicate: same fix as demo/durations {up[up_key][:10]}" in (
            text
        )
    records = [json.loads(line) for line in out.read_text().splitlines()]
    by_sha = {r["sha"]: r for r in records}
    assert by_sha[fork["spaces"]]["ledger"] == {"matches": [], "status": "new"}
    match = by_sha[fork["fix_b"]]["ledger"]["matches"][0]
    assert (match["source"], match["sha"], match["exact"], match["owner"]) == (
        "ledger",
        up["fix_b"],
        True,
        "alice",
    )


def test_new_only_leaves_out_duplicates_and_keeps_ranks(
    demo: tuple[Path, dict[str, dict[str, str]]],
    claimed: tuple[Path, Path, dict[str, dict[str, str]]],
    tmp_path: Path,
) -> None:
    root, _ = demo
    ledger, _, shas = claimed
    out = tmp_path / "new.jsonl"
    text = mine(
        str(root / "upstream"), "--rev", "release", "--ledger", str(ledger), "--new-only",
        "--out", str(out), "--repo-name", "demo/durations",
    )  # fmt: skip
    assert "showing the 1 new candidates (--new-only)" in text
    rows = [line for line in text.splitlines() if line.startswith("   ")]
    assert len(rows) == 1
    assert rows[0].startswith("   2 ")
    assert "new      Accept upper-case units" in rows[0]
    (record,) = [json.loads(line) for line in out.read_text().splitlines()]
    assert (record["rank"], record["sha"]) == (2, shas["upstream"]["upper"])
    explained = runner.invoke(
        app,
        [
            *("mine", str(root / "upstream"), "--rev", "release", "--ledger", str(ledger)),
            *("--new-only", "--explain", "1"),
        ],
    )
    assert "#2 " + shas["upstream"]["upper"][:10] in explained.stdout


def test_ledger_add_refuses_what_is_taken_and_check_reports_it(
    claimed: tuple[Path, Path, dict[str, dict[str, str]]], tmp_path: Path
) -> None:
    ledger, exported, shas = claimed
    fix_a = shas["upstream"]["fix_a"]
    again = runner.invoke(
        app, ["ledger", "add", str(ledger), str(exported), "--sha", fix_a[:7], "--owner", "bob"]
    )
    assert again.exit_code == 1
    assert again.stdout.splitlines() == [
        f"refused  #1 demo/durations {fix_a[:10]}  duplicate: already in the ledger "
        "(claimed by alice on 2026-01-02)",
        f"0 added, 1 refused: {ledger}",
    ]
    checked = runner.invoke(app, ["ledger", "check", str(ledger), str(exported), "--json"])
    assert checked.exit_code == 1
    first = json.loads(checked.stdout.splitlines()[0])
    assert (first["rank"], first["status"], first["matches"][0]["owner"]) == (
        1,
        "duplicate",
        "alice",
    )
    # A ledger that does not exist is an error, not an empty ledger: a mistyped path
    # would otherwise report every candidate as new.
    fresh = tmp_path / "fresh.sqlite3"
    typo = runner.invoke(app, ["ledger", "check", str(fresh), str(exported)])
    assert typo.exit_code == 2
    assert f"error: {fresh}: no such ledger (ledger add creates one)" in typo.stderr
    assert not fresh.exists()
    fresh.write_bytes(b"")  # an empty file is an empty ledger
    clean = runner.invoke(app, ["ledger", "check", str(fresh), str(exported)])
    assert clean.exit_code == 0, clean.output
    assert clean.stdout.splitlines()[0] == f"#1 demo/durations {fix_a[:10]}  new"


def test_ledger_add_selects_candidates(
    claimed: tuple[Path, Path, dict[str, dict[str, str]]], tmp_path: Path
) -> None:
    _, exported, shas = claimed
    ledger = tmp_path / "other.sqlite3"
    top = runner.invoke(
        app, ["ledger", "add", str(ledger), str(exported), "--top", "1", "--status", "proposed"]
    )
    assert top.exit_code == 0, top.output
    assert top.stdout.splitlines()[0].endswith("  proposed")
    none = runner.invoke(app, ["ledger", "add", str(ledger), str(exported), "--sha", "ffff"])
    assert none.exit_code == 2
    assert "no candidate in" in none.output
    bad = runner.invoke(app, ["ledger", "add", str(ledger), str(exported), "--status", "done"])
    assert bad.exit_code == 2
    assert "--status must be proposed or claimed" in bad.output
    rest = runner.invoke(
        app,
        ["ledger", "add", str(ledger), str(exported), "--allow-overlap", "--owner", "carol"],
    )
    assert rest.exit_code == 1
    assert rest.stdout.splitlines()[1:3] == [
        f"added    #2 demo/durations {shas['upstream']['fix_b'][:10]}  claimed",
        f"added    #3 demo/durations {shas['upstream']['base'][:10]}  claimed",
    ]


def test_ledger_list(claimed: tuple[Path, Path, dict[str, dict[str, str]]]) -> None:
    ledger, _, shas = claimed
    text = runner.invoke(app, ["ledger", "list", str(ledger)]).stdout.splitlines()
    assert text[0] == f"{ledger}: 3 entries (schema version 1)"
    assert text[1].split() == [
        "first", "seen", "status", "owner", "repo", "sha", "hunks", "fingerprint", "subject",
    ]  # fmt: skip
    fix_a = shas["upstream"]["fix_a"][:10]
    assert text[2].split()[:6] == ["2026-01-02", "claimed", "alice", "demo/durations", fix_a, "3"]
    records = [
        json.loads(line)
        for line in runner.invoke(
            app, ["ledger", "list", str(ledger), "--repo", "demo/durations", "--json"]
        ).stdout.splitlines()
    ]
    assert [r["sha"] for r in records] == [
        shas["upstream"][key] for key in ("fix_a", "fix_b", "base")
    ]
    empty = runner.invoke(app, ["ledger", "list", str(ledger), "--repo", "nope"])
    assert empty.stdout == f"{ledger}: 0 entries (schema version 1)\n"


def test_ledger_errors_are_plain_and_exit_2(tmp_path: Path) -> None:
    """Exit code 1 means "not new" or "refused"; every error exits with 2."""
    text = tmp_path / "notes.txt"
    text.write_text("plain text, not SQLite\n" * 10)
    result = runner.invoke(app, ["ledger", "list", str(text)])
    assert result.exit_code == 2
    assert "cannot use as a ledger: file is not a database" in result.stderr
    bad = tmp_path / "bad.jsonl"
    bad.write_text('{"schema_version": 2}\n')
    checked = runner.invoke(app, ["ledger", "check", str(tmp_path / "l.sqlite3"), str(bad)])
    assert checked.exit_code == 2
    assert "schema_version 2, expected 3" in checked.stderr
    unreadable = runner.invoke(
        app, ["ledger", "check", str(text), str(tmp_path / "does-not-exist.jsonl")]
    )
    assert unreadable.exit_code == 2
    assert "does-not-exist.jsonl: cannot read" in unreadable.stderr
    recording = ROOT / "examples" / "tomli" / "history.jsonl.gz"
    mined = runner.invoke(app, ["mine", "--history", str(recording), "--ledger", str(text)])
    assert mined.exit_code == 2
    assert "cannot use as a ledger" in mined.stderr
    missing = tmp_path / "typo-ledger.sqlite3"
    for args in (
        ["mine", "--history", str(recording), "--ledger", str(missing)],
        ["ledger", "list", str(missing)],
    ):
        result = runner.invoke(app, args)
        assert result.exit_code == 2, result.output
        assert f"{missing}: no such ledger (ledger add creates one)" in result.stderr
    assert not missing.exists()
    new_only = runner.invoke(app, ["mine", "--history", str(bad), "--new-only"])
    assert new_only.exit_code == 2
    assert "--new-only needs --ledger" in new_only.output


def test_a_ledger_that_cannot_be_written_is_a_plain_error(
    demo: tuple[Path, dict[str, dict[str, str]]],
    claimed: tuple[Path, Path, dict[str, dict[str, str]]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only file or a lock held longer than the timeout: no traceback, exit 2."""
    root, _ = demo
    ledger, _, shas = claimed
    exported = tmp_path / "fork.jsonl"
    mine(str(root / "fork"), "--repo-name", "demo/fork", "--out", str(exported))
    fix_b = shas["fork"]["spaces"]  # the fork's own fix: not in the ledger
    locked = tmp_path / "locked.sqlite3"
    locked.write_bytes(ledger.read_bytes())
    other = sqlite3.connect(locked, isolation_level=None)
    other.execute("BEGIN IMMEDIATE")
    other.execute("UPDATE entries SET owner = 'other'")
    monkeypatch.setattr(cli, "open_ledger", partial(ledger_module.open_ledger, timeout=0.05))
    try:
        result = runner.invoke(
            app, ["ledger", "add", str(locked), str(exported), "--sha", fix_b[:7], "--owner", "bob"]
        )
    finally:
        other.execute("ROLLBACK")
        other.close()
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert f"error: {locked}: cannot write: database is locked" in result.stderr
    read_only = tmp_path / "ro.sqlite3"
    read_only.write_bytes(ledger.read_bytes())
    read_only.chmod(0o444)
    if os.access(read_only, os.W_OK):
        pytest.skip("file permissions are not enforced here (running as root?)")
    result = runner.invoke(
        app, ["ledger", "add", str(read_only), str(exported), "--sha", fix_b[:7], "--owner", "bob"]
    )
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert f"error: {read_only}: cannot write: attempt to write a readonly database" in (
        result.stderr
    )
    # Reading a read-only ledger still works.
    listed = runner.invoke(app, ["ledger", "list", str(read_only)])
    assert listed.exit_code == 0, listed.output
    assert "3 entries" in listed.stdout


def test_a_fix_to_the_spaces_inside_a_string_is_fingerprinted(
    git_repo: GitRepo, tmp_path: Path
) -> None:
    """Regression: such a fix had no fingerprint, --new-only dropped it, ledger add refused it."""
    src, test = "src/pkg/fmt.py", "tests/test_fmt.py"
    code = 'def pair(a, b):\n    return f"{a}  {b}"\n'
    check = 'from pkg.fmt import pair\n\n\ndef test_pair():\n    assert pair(1, 2) == "1  2"\n'
    git_repo.commit("Add pair()", {src: code, test: check})
    fixed = git_repo.commit(
        "Fix double space in pair() output (fixes #4)",
        {src: code.replace("{a}  {b}", "{a} {b}"), test: check.replace('"1  2"', '"1 2"')},
    )
    ledger = tmp_path / "ledger.sqlite3"
    ledger.write_bytes(b"")
    out = tmp_path / "all.jsonl"
    text = mine(
        str(git_repo.root), "--repo-name", "demo/ws", "--ledger", str(ledger), "--new-only",
        "--out", str(out),
    )  # fmt: skip
    assert f"ledger {ledger}: 2 new" in text
    assert "unknown" not in text
    assert "showing the 2 new candidates (--new-only)" in text
    (first, _) = [json.loads(line) for line in out.read_text().splitlines()]
    assert first["sha"] == fixed
    assert first["fingerprint"]["version"] == 2
    assert len(first["fingerprint"]["hunks"]) == 2
    added = runner.invoke(app, ["ledger", "add", str(ledger), str(out), "--sha", fixed[:8]])
    assert added.exit_code == 0, added.output
    assert added.stdout.splitlines()[0] == f"added    #1 demo/ws {fixed[:10]}  claimed"
    explained = runner.invoke(app, ["explain", fixed, "--repo", str(git_repo.root)])
    assert "patch    fingerprint " in explained.stdout
    assert "(2 source and test hunks)" in explained.stdout


def test_the_builder_refuses_an_existing_directory(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="already exists"):
        BUILDER.build(tmp_path)
