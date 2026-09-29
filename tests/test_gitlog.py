from __future__ import annotations

import io
from pathlib import Path

import pytest

from commitminer import gitlog
from commitminer.fingerprint import hunk_hash
from commitminer.gitlog import (
    MARKER,
    GitError,
    head_sha,
    log_command,
    parse_log,
    split_stream,
    stream_git,
    walk,
)
from commitminer.models import Commit, FileChange, PatchStats
from gitrepo import GitRepo, lines, unhashed


def _header(sha: str, parents: str, message: str) -> bytes:
    return f"\0{MARKER}\0{sha}\0{parents}\0{'2024-01-01T00:00:00+00:00'}\0{message}\0".encode()


# --- parser on hand-written output ---------------------------------------------------


def test_parse_empty_output_yields_nothing() -> None:
    assert list(parse_log(b"")) == []


def test_parse_plain_rename_and_binary_entries() -> None:
    data = (
        _header("b" * 40, "a" * 40, "fix\n")
        + b"\n3\t1\tsrc/pkg/mod.py\0"
        + b"2\t0\t\0old name.py\0new name.py\0"
        + b"-\t-\tlogo.png\0"
    )
    (commit,) = parse_log(data)
    assert commit.sha == "b" * 40
    assert commit.parents == ("a" * 40,)
    assert commit.files == (
        FileChange("src/pkg/mod.py", 3, 1),
        FileChange("new name.py", 2, 0, old_path="old name.py"),
        FileChange("logo.png", None, None),
    )


def test_parse_path_equal_to_marker_is_consumed_as_a_path() -> None:
    data = _header("c" * 40, "", "x\n") + f"\n1\t0\t\0{MARKER}\0renamed\0".encode()
    (commit,) = parse_log(data)
    assert commit.files == (FileChange("renamed", 1, 0, old_path=MARKER),)
    assert commit.parents == ()


def test_parse_non_utf8_path_round_trips_through_surrogateescape() -> None:
    data = _header("d" * 40, "", "x\n") + b"\n1\t0\tcaf\xe9.py\0"
    (commit,) = parse_log(data)
    assert commit.files[0].path.encode("utf-8", "surrogateescape") == b"caf\xe9.py"


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"garbage\0", "unexpected token"),
        (f"\0{MARKER}\0sha\0".encode(), "truncated commit header"),
        (_header("e" * 40, "", "x") + b"\nnot numstat\0", "bad numstat entry"),
        (_header("e" * 40, "", "x") + b"\n1\t1\t\0only-old\0", "truncated rename"),
    ],
)
def test_parse_rejects_malformed_output(data: bytes, message: str) -> None:
    with pytest.raises(GitError, match=message):
        list(parse_log(data))


PATCH = b"diff --git a/a.py b/a.py\n@@ -1 +1,2 @@\n-x = 1\n+x = 2\n+assert x\n"


def test_parse_patch_section_is_matched_to_numstat() -> None:
    data = _header("f" * 40, "", "fix\n") + b"\n2\t1\ta.py\0\0" + PATCH
    (commit,) = parse_log(data)
    (change,) = commit.files
    assert unhashed(change) == FileChange("a.py", 2, 1, patch=PatchStats(1, 1, 2, 1, asserts=1))
    assert change.patch is not None
    assert change.patch.hunk_hashes == (hunk_hash(["x = 1"], ["x = 2", "assert x"]),)


@pytest.mark.parametrize(
    ("numstat", "patch", "message"),
    [
        (b"\n3\t1\ta.py\0", PATCH, "numstat says \\+3 -1, the patch has \\+2 -1"),
        (b"\n2\t1\ta.py\0" + b"1\t0\tb.py\0", PATCH, "2 numstat entries but 1 file patches"),
        (b"\n2\t1\ta.py\0", b"diff --git a/a.py b/a.py\n@@ nonsense\n", "bad patch in"),
    ],
)
def test_parse_rejects_patches_that_do_not_match(
    numstat: bytes, patch: bytes, message: str
) -> None:
    data = _header("f" * 40, "", "fix\n") + numstat + b"\0" + patch
    with pytest.raises(GitError, match=message):
        list(parse_log(data))


def test_parse_two_commits_with_patches_and_an_empty_commit() -> None:
    first = _header("a" * 40, "b" * 40, "one\n") + b"\n2\t1\ta.py\0\0" + PATCH
    empty = _header("b" * 40, "", "empty\n")
    shas = [c.sha[0] for c in parse_log(first + empty)]
    assert shas == ["a", "b"]


@pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64])
def test_split_stream_matches_bytes_split(chunk: int) -> None:
    for data in (b"", b"\0", b"a\0\0bc\0", b"abc", b"\0x\0yz\0\0"):
        assert list(split_stream(io.BytesIO(data), chunk)) == data.split(b"\0")


def test_stream_git_reports_failures_after_reading() -> None:
    with (
        pytest.raises(GitError, match="git exited with 1: boom"),
        stream_git(["sh", "-c", "printf 'a\\0b'; echo boom >&2; exit 1"]) as tokens,
    ):
        assert list(tokens) == [b"a", b"b"]


def test_stream_git_times_out() -> None:
    with pytest.raises(GitError, match="timed out"), stream_git(["sleep", "5"], 0.05) as tokens:
        list(tokens)


def _fail_while_reading(args: list[str], timeout: float, read_all: bool) -> None:
    with stream_git(args, timeout) as tokens:
        if read_all:
            list(tokens)
        else:
            next(tokens)
        raise ValueError("the parser gave up")


def test_stream_git_times_out_while_the_caller_fails() -> None:
    with pytest.raises(GitError, match="timed out"):
        _fail_while_reading(["sleep", "5"], 0.05, read_all=True)


def test_stream_git_kills_git_when_the_caller_stops_early() -> None:
    with pytest.raises(ValueError, match="gave up"):
        _fail_while_reading(["sh", "-c", "while :; do printf 'a\\0'; done"], 60, False)


def test_stream_git_reports_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(GitError, match="not found"), stream_git(["git", "--version"]):
        pass


def test_log_command_is_fixed_and_guards_the_revision() -> None:
    cmd = log_command(Path("/r"), "--output=/tmp/x", 5)
    assert cmd[:3] == ["git", "-C", "/r"]
    assert "--no-merges" in cmd
    assert "-M" in cmd
    assert "-z" in cmd
    assert "--max-count=5" in cmd
    assert cmd[-2:] == ["--end-of-options", "--output=/tmp/x"]


def test_log_command_rejects_non_positive_max_count() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        log_command(Path("/r"), max_count=0)


# --- walker on real synthetic repositories ------------------------------------------


def test_walk_edge_cases_in_a_real_repository(git_repo: GitRepo) -> None:
    root = git_repo.commit("root: empty", allow_empty=True)
    added = git_repo.commit(
        "add files\n\nWith a body.\nSecond line.",
        {"my file.py": lines(6), "bin.dat": b"\x00\x01\x02", "caf\u00e9.py": "x\n"},
    )
    (git_repo.root / "my file.py").rename(git_repo.root / "new name.py")
    git_repo.write({"new name.py": lines(6) + "extra\n"})
    renamed = git_repo.commit("rename with an edit")
    empty_msg = git_repo.commit("", {"tests/test_x.py": "assert True\n"})

    commits = walk(git_repo.root)

    assert [c.sha for c in commits] == [empty_msg, renamed, added, root]
    assert commits[3].parents == ()
    assert commits[3].files == ()
    assert commits[2].parents == (root,)
    assert commits[2].subject == "add files"
    assert commits[2].body == "With a body.\nSecond line."
    assert {unhashed(f) for f in commits[2].files} == {
        FileChange("bin.dat", None, None),
        FileChange("caf\u00e9.py", 1, 0, patch=PatchStats(1, 1, 1, 0)),
        FileChange("my file.py", 6, 0, patch=PatchStats(1, 1, 6, 0)),
    }
    assert tuple(map(unhashed, commits[1].files)) == (
        FileChange("new name.py", 1, 0, old_path="my file.py", patch=PatchStats(1, 1, 1, 0)),
    )
    assert commits[0].message == ""
    assert tuple(map(unhashed, commits[0].files)) == (
        FileChange("tests/test_x.py", 1, 0, patch=PatchStats(1, 1, 1, 0, asserts=1)),
    )
    assert commits[0].date == "2024-01-01T03:00:00+00:00"


def test_walk_skips_merge_commits(git_repo: GitRepo) -> None:
    git_repo.commit("base", {"a.py": "a\n"})
    git_repo.git("checkout", "-q", "-b", "side")
    side = git_repo.commit("side change", {"b.py": "b\n"})
    git_repo.git("checkout", "-q", "main")
    main = git_repo.commit("main change", {"c.py": "c\n"})
    git_repo.git("merge", "-q", "--no-ff", "-m", "merge side", "side")
    shas = [c.sha for c in walk(git_repo.root)]
    assert len(shas) == 3
    assert {side, main} <= set(shas)


def test_walk_honours_revision_ranges_and_max_count(git_repo: GitRepo) -> None:
    first = git_repo.commit("one", {"a.py": "1\n"})
    second = git_repo.commit("two", {"a.py": "2\n"})
    third = git_repo.commit("three", {"a.py": "3\n"})
    assert [c.sha for c in walk(git_repo.root, f"{first}..HEAD")] == [third, second]
    assert [c.sha for c in walk(git_repo.root, max_count=1)] == [third]
    assert head_sha(git_repo.root) == third


def test_walk_ignores_repository_config_that_would_change_output(git_repo: GitRepo) -> None:
    git_repo.commit("one", {"a.py": "1\n"})
    git_repo.git("config", "log.showSignature", "true")
    git_repo.git("config", "color.ui", "always")
    (commit,) = walk(git_repo.root)
    assert commit.subject == "one"


def test_walk_reports_git_errors(tmp_path: Path) -> None:
    with pytest.raises(GitError, match="git exited with"):
        walk(tmp_path)


def test_run_git_reports_missing_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "")
    with pytest.raises(GitError, match="not found"):
        gitlog.run_git(["git", "--version"])


def test_run_git_reports_timeouts() -> None:
    with pytest.raises(GitError, match="timed out"):
        gitlog.run_git(["sleep", "5"], timeout=0.01)


def test_commit_subject_joins_the_first_paragraph() -> None:
    commit = Commit("s", (), "d", "Fix the\nparser\n\nBody text\n", ())
    assert commit.subject == "Fix the parser"
    assert commit.body == "Body text"
    assert commit.base is None
    assert FileChange("x", None, None).changed_lines == 0
    assert FileChange("x", None, None).binary
    assert not FileChange("x", 0, 0).binary
    assert FileChange("x", 2, 3).changed_lines == 5
