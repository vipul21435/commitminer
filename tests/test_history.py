from __future__ import annotations

import json
from pathlib import Path

import pytest

from commitminer.gitlog import normalize_date, walk
from commitminer.history import (
    HistoryError,
    commit_from_json,
    parse_history,
    read_history,
    write_history,
)
from commitminer.models import Commit, FileChange, PatchStats
from gitrepo import GitRepo, lines

SAMPLE = Commit(
    sha="a" * 40,
    parents=("b" * 40,),
    date="2024-01-01T00:00:00+02:00",
    message="Fix caf\u00e9 parsing (#12)\n",
    files=(
        FileChange("src/pkg/mod.py", 3, 1),
        FileChange("new.py", 1, 0, old_path="old.py"),
        FileChange("img.png", None, None),
        FileChange("caf\udce9.py", 1, 1),
        FileChange("src/lib.rs", 9, 2, signals=("rust-inline-tests", "rust-tests-added")),
        FileChange(
            "src/pkg/api.py",
            7,
            3,
            patch=PatchStats(3, 2, 5, 3, test_added=1, test_deleted=1, asserts=2, api=("def a",)),
        ),
        FileChange("tests/data/case.toml", 4, 0, patch=PatchStats(1, 1, 4, 0)),
    ),
)


def test_round_trip_is_exact_and_ascii(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "history.jsonl"
    header = write_history(path, [SAMPLE], repo="demo/repo", url="https://x.invalid", head="h")
    assert header.commits == 1
    path.read_text(encoding="ascii")  # non-ASCII text is escaped
    read_header, commits = read_history(path)
    assert read_header == header
    assert commits == [SAMPLE]


def test_recording_a_real_walk_replays_identically(git_repo: GitRepo, tmp_path: Path) -> None:
    git_repo.commit("one", {"src/a.py": lines(3), "tests/test_a.py": lines(2)})
    git_repo.commit("two", {"src/a.py": lines(4)})
    walked = walk(git_repo.root)
    first, second = tmp_path / "1.jsonl", tmp_path / "2.jsonl"
    write_history(first, walked, repo="r")
    write_history(second, read_history(first)[1], repo="r")
    assert first.read_bytes() == second.read_bytes()
    assert read_history(first)[1] == walked


def test_gzip_recordings_round_trip_and_are_reproducible(tmp_path: Path) -> None:
    first, second = tmp_path / "a.jsonl.gz", tmp_path / "b.jsonl.gz"
    write_history(first, [SAMPLE], repo="r")
    write_history(second, [SAMPLE], repo="r")
    assert first.read_bytes() == second.read_bytes()
    assert first.read_bytes()[:2] == b"\x1f\x8b"
    assert read_history(first)[1] == [SAMPLE]


def _header(**overrides: object) -> str:
    base: dict[str, object] = {
        "format": "commitminer-history",
        "version": 1,
        "repo": "r",
        "url": None,
        "head": None,
        "commits": 0,
    }
    base.update(overrides)
    return json.dumps(base)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ([], "empty history"),
        (["{"], "invalid JSON"),
        (['{"format": "other"}'], "not a commitminer-history"),
        ([_header(version=2)], "unsupported version"),
        ([_header(url=1)], "url must be"),
        ([_header(head=1)], "head must be"),
        ([_header(commits=None)], "commits is required"),
        ([_header(commits=-1)], "non-negative integer"),
        ([_header(repo=None)], "repo: expected a string"),
        ([_header(commits=1)], "header says 1 commits but the file has 0"),
        ([_header(commits=1), "{"], "line 2: invalid JSON"),
    ],
)
def test_parse_history_rejects_bad_files(text: list[str], message: str) -> None:
    with pytest.raises(HistoryError, match=message):
        parse_history(text)


def test_blank_lines_after_the_header_are_ignored() -> None:
    header, commits = parse_history([_header(), "", "  "])
    assert header.repo == "r"
    assert commits == []


@pytest.mark.parametrize(
    ("record", "message"),
    [
        ([], "expected an object"),
        ({"sha": "s", "parents": [], "date": "d"}, "missing field 'files'"),
        ({"sha": 1, "parents": [], "date": "d", "message": "", "files": []}, "sha: expected"),
        ({"sha": "s", "parents": "p", "date": "d", "message": "", "files": []}, "must be lists"),
        ({"sha": "s", "parents": [], "date": "d", "message": "", "files": [1]}, "with a path"),
        (
            {"sha": "s", "parents": [], "date": "d", "message": "", "files": [{"path": "p"}]},
            None,
        ),
        (
            {
                "sha": "s",
                "parents": [],
                "date": "d",
                "message": "",
                "files": [{"path": "p", "added": True}],
            },
            "non-negative integer",
        ),
        (
            {
                "sha": "s",
                "parents": [],
                "date": "d",
                "message": "",
                "files": [{"path": "p", "old_path": 3}],
            },
            "old_path: expected a string",
        ),
        (
            {
                "sha": "s",
                "parents": [],
                "date": "d",
                "message": "",
                "files": [{"path": "p", "signals": "minified"}],
            },
            r"files\[0\].signals: expected a list",
        ),
        (
            {
                "sha": "s",
                "parents": [],
                "date": "d",
                "message": "",
                "files": [{"path": "p", "signals": [1]}],
            },
            "signals: expected a string",
        ),
    ],
)
def test_commit_from_json_validates_fields(record: object, message: str | None) -> None:
    if message is None:
        commit = commit_from_json(record)
        assert commit.files == (FileChange("p", None, None),)
        return
    with pytest.raises(HistoryError, match=message):
        commit_from_json(record)


def test_normalize_date_spells_utc_one_way() -> None:
    assert normalize_date("2024-01-01T00:00:00Z") == "2024-01-01T00:00:00+00:00"
    assert normalize_date("2024-01-01T00:00:00+02:00") == "2024-01-01T00:00:00+02:00"


def test_signals_are_recorded_only_when_present(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    write_history(path, [SAMPLE], repo="r")
    record = json.loads(path.read_text(encoding="ascii").splitlines()[1])
    assert [f.get("signals") for f in record["files"]] == [
        None,
        None,
        None,
        None,
        ["rust-inline-tests", "rust-tests-added"],
        None,
        None,
    ]


def test_patch_fields_are_recorded_only_when_they_differ_from_the_defaults(
    tmp_path: Path,
) -> None:
    path = tmp_path / "h.jsonl"
    write_history(path, [SAMPLE], repo="r")
    record = json.loads(path.read_text(encoding="ascii").splitlines()[1])
    assert [f.get("patch") for f in record["files"][-2:]] == [
        {
            "hunks": 3,
            "code_hunks": 2,
            "code_added": 5,
            "test_added": 1,
            "test_deleted": 1,
            "asserts": 2,
            "api": ["def a"],
        },
        {"hunks": 1},
    ]


def _with_patch(patch: object, added: int | None = 1) -> dict[str, object]:
    return {
        "sha": "s",
        "parents": [],
        "date": "d",
        "message": "",
        "files": [{"path": "p", "added": added, "deleted": 0, "patch": patch}],
    }


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (_with_patch([]), r"files\[0\].patch: expected an object"),
        (_with_patch({"hunks": 1, "lines": 2}), "unknown key 'lines'"),
        (_with_patch({"hunks": 1}, added=None), "a binary file has no patch"),
        (_with_patch({"hunks": 1, "api": "def a"}), r"patch.api: expected a list"),
        (_with_patch({"hunks": 1, "api": [1]}), r"patch.api: expected a string"),
        (_with_patch({}), r"patch.hunks: required"),
        (_with_patch({"hunks": 1, "asserts": -1}), r"patch.asserts: expected a non-negative"),
    ],
)
def test_patch_records_are_validated(record: object, message: str) -> None:
    with pytest.raises(HistoryError, match=message):
        commit_from_json(record)
