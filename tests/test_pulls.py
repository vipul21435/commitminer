"""Merged pull requests as commits: linked issues, file patches, the walker (offline)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from commitminer.export import candidate_to_json, render_ledger, render_table
from commitminer.fixtures import ReplayTransport
from commitminer.github import GitHubClient, GitHubError
from commitminer.ledger import Status, Verdict
from commitminer.models import FileChange, PatchStats, PullRequest
from commitminer.patch import Hunk, PatchError, hunks_from_unified, parse_patch
from commitminer.pulls import file_change, linked_issues, merged_pulls, pull_commit, pull_sha
from commitminer.scoring import Candidate, fix_keyword, linked_reference, mine
from fixturefiles import API, FakeClock, reply, write_fixture
from gitrepo import GitRepo, lines, unhashed

# --- linked issues ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (["Fixes #12"], ("#12",)),
        (["closes: #3, resolves #1 and fixes #3"], ("#1", "#3")),
        (["Resolved o/r#4", "fixed Other/Repo#9"], ("#4", "Other/Repo#9")),
        (["Closes https://github.com/o/r/issues/7"], ("#7",)),
        (["closes https://github.com/x/y/issues/8"], ("x/y#8",)),
        (["See #5", "fix #", "prefixes #6", "Closes https://github.com/o/r/pull/2"], ()),
        (["FIXES #10\r\n"], ("#10",)),
    ],
)
def test_linked_issues(texts: list[str], expected: tuple[str, ...]) -> None:
    assert linked_issues(texts, "o/r") == expected


# --- unified patches with context ----------------------------------------------------


def test_hunks_from_unified_match_git_unified_zero(git_repo: GitRepo) -> None:
    before = lines(30)
    after = (
        "new first line\n"
        + before.replace("line 5\n", "line five\n")
        .replace("line 7\n", "")
        .replace("line 9\n", "line 9\nextra\n")
        .replace("line 20\nline 21\n", "twenty\n")
        .removesuffix("line 29\n")
        + "last, no newline"
    )
    git_repo.commit("base", {"f.txt": before})
    git_repo.commit("edit", {"f.txt": after})
    options = ["--diff-algorithm=myers", "--indent-heuristic", "--no-color", "HEAD~1", "HEAD"]
    zero = git_repo.git("diff", "--unified=0", "--inter-hunk-context=0", *options)
    context = git_repo.git("diff", "--unified=3", *options)
    (expected,) = parse_patch(zero.encode())
    github_style = context[context.index("@@") :]
    assert hunks_from_unified(github_style) == expected.hunks
    assert len(expected.hunks) == 6


def test_hunks_from_unified_numbers_empty_sides_like_git() -> None:
    new_file = "@@ -0,0 +1,2 @@\n+a\n+b"
    assert hunks_from_unified(new_file) == (Hunk(0, 1, (), ("a", "b")),)
    gone = "@@ -1,2 +0,0 @@\n-a\n-b\n"
    assert hunks_from_unified(gone) == (Hunk(1, 0, ("a", "b"), ()),)
    swapped = "@@ -1,2 +1,3 @@\n+x\r\n-a\n+y\n b\n"
    assert hunks_from_unified(swapped) == (
        Hunk(0, 1, (), ("x",)),
        Hunk(1, 2, ("a",), ("y",)),
    )
    assert hunks_from_unified("@@ -1 +1 @@\n-a\n+b\n\\ No newline at end of file") == (
        Hunk(1, 1, ("a",), ("b",)),
    )
    assert hunks_from_unified("@@ -3,2 +3,2 @@\n x\n\n") == ()


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("garbage", "expected a hunk header"),
        ("@@ -1 +1 @@\n*a", "unexpected line"),
        ("@@ -1 +1 @@\n-a\n-b", "more lines than its header"),
        ("@@ -1,3 +1,3 @@\n a", "ends inside a hunk"),
    ],
)
def test_hunks_from_unified_rejects_malformed_patches(text: str, message: str) -> None:
    with pytest.raises(PatchError, match=message):
        hunks_from_unified(text)


# --- pull-request files --------------------------------------------------------------


def entry(**fields: Any) -> dict[str, Any]:
    return {"filename": "src/pkg/mod.py", "status": "modified", **fields}


def test_file_change_measures_the_patch() -> None:
    change = file_change(
        entry(additions=2, deletions=1, patch="@@ -1,2 +1,3 @@\n-x = 1\n+x = 2\n+y = 3\n z = 4")
    )
    assert unhashed(change) == FileChange("src/pkg/mod.py", 2, 1, patch=PatchStats(1, 1, 2, 1))


def test_file_change_without_a_patch() -> None:
    renamed = file_change(
        entry(filename="b.py", previous_filename="a.py", status="renamed", additions=0, deletions=0)
    )
    assert renamed == FileChange("b.py", 0, 0, "a.py", patch=PatchStats(0, 0, 0, 0, hunk_hashes=()))
    assert file_change(entry(filename="logo.png", additions=0, deletions=0)).binary
    mode = file_change(entry(status="changed", additions=0, deletions=0))
    assert (mode.binary, mode.patch) == (False, PatchStats(0, 0, 0, 0, hunk_hashes=()))
    large = file_change(entry(additions=9000, deletions=0))
    assert (large.added, large.patch) == (9000, None)
    truncated = file_change(entry(additions=5, deletions=0, patch="@@ -0,0 +1 @@\n+x"))
    assert (truncated.added, truncated.patch) == (5, None)


@pytest.mark.parametrize(
    ("item", "message"),
    [
        ([], "expected an object"),
        (entry(filename=None), "filename: expected a string"),
        (entry(additions=-1, deletions=0), "additions: expected a count"),
        (entry(additions=True, deletions=0), "additions: expected a count"),
        (entry(additions=1, deletions=0, patch="nonsense"), "expected a hunk header"),
    ],
)
def test_file_change_rejects_malformed_entries(item: Any, message: str) -> None:
    with pytest.raises(GitHubError, match=message):
        file_change(item)


# --- pull requests as commits --------------------------------------------------------

BASE, HEAD, MERGE = "b" * 40, "c" * 40, "d" * 40


def pull(number: int = 7, merged_at: str | None = "2024-03-01T10:00:00Z", **fields: Any) -> dict:
    return {
        "number": number,
        "title": f"Fix parsing of empty keys (#{number})",
        "body": "The parser crashed.\r\n\r\nFixes #3",
        "html_url": f"https://github.com/o/r/pull/{number}",
        "merged_at": merged_at,
        "merge_commit_sha": MERGE,
        "base": {"ref": "main", "sha": BASE},
        "head": {"ref": "fix", "sha": HEAD},
        "labels": [{"name": "type: bug"}, {"name": "parser"}],
        **fields,
    }


COMMITS = [
    {"sha": "e" * 40, "commit": {"message": "Fix empty keys\n\nResolves o/r#4"}},
    {"sha": "f" * 40, "commit": {"message": "Add a test"}},
]
FILES = [
    entry(additions=1, deletions=1, patch="@@ -1 +1 @@\n-x = 1\n+x = 2"),
    entry(
        filename="tests/test_mod.py",
        status="added",
        additions=2,
        deletions=0,
        patch="@@ -0,0 +1,2 @@\n+def test_x():\n+    assert x == 2",
    ),
]


def test_pull_commit() -> None:
    commit = pull_commit(pull(), COMMITS, FILES, "o/r")
    assert commit.sha == MERGE
    assert commit.parents == (BASE,)
    assert commit.date == "2024-03-01T10:00:00+00:00"
    assert commit.message == "Fix parsing of empty keys (#7)\n\nThe parser crashed.\n\nFixes #3\n"
    assert commit.pull_request == PullRequest(
        number=7,
        url="https://github.com/o/r/pull/7",
        title="Fix parsing of empty keys (#7)",
        merged_at="2024-03-01T10:00:00+00:00",
        base_ref="main",
        base_sha=BASE,
        head_sha=HEAD,
        merge_commit_sha=MERGE,
        labels=("parser", "type: bug"),
        linked_issues=("#3", "#4"),
        commits=("e" * 40, "f" * 40),
    )
    assert [f.path for f in commit.files] == ["src/pkg/mod.py", "tests/test_mod.py"]
    assert commit.files[1].patch is not None
    assert commit.files[1].patch.asserts == 1


def test_pull_commit_without_body_labels_or_merge_sha() -> None:
    commit = pull_commit(
        pull(body=None, labels=None, merge_commit_sha=None, title="Tidy"), [], [], "o/r"
    )
    assert commit.sha == HEAD
    assert commit.message == "Tidy\n"
    assert commit.pull_request is not None
    assert (commit.pull_request.labels, commit.pull_request.linked_issues) == ((), ())


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({"number": "7"}, "number: expected a count"),
        ({"merged_at": None}, "merged_at: expected a string"),
        ({"base": None}, "base: expected an object"),
        ({"labels": "bug"}, "labels: expected a list"),
        ({"labels": [{"name": 1}]}, "labels.name: expected a string"),
        ({"merge_commit_sha": 5}, "merge_commit_sha: expected a string"),
    ],
)
def test_pull_commit_rejects_malformed_pulls(fields: dict[str, Any], message: str) -> None:
    with pytest.raises(GitHubError, match=message):
        pull_commit(pull(**fields), [], [], "o/r")


def test_pull_request_metadata_feeds_the_score() -> None:
    commit = pull_commit(pull(title="Handle empty keys"), COMMITS, FILES, "o/r")
    (candidate,) = mine([commit]).candidates
    features = {f.name: f for f in candidate.features}
    assert features["linked_reference"].value == 1.0
    assert features["linked_reference"].detail == "pull request #7 closes #3, #4"
    assert features["fix_keyword"].value == 1.0
    assert features["fix_keyword"].detail == "label 'type: bug'"
    record = candidate_to_json(candidate, 1, "o/r")
    assert record["schema_version"] == 6
    assert record["base"] == BASE
    assert record["pull_request"]["number"] == 7
    assert record["pull_request"]["linked_issues"] == ["#3", "#4"]
    table = render_table(mine([commit]), 5)
    assert table.splitlines()[0].split()[3] == "pull"
    assert table.splitlines()[1].split()[4] == "#7"
    verdict = Verdict(Status.UNKNOWN, ())
    assert "#1 #7 unknown" in render_ledger(mine([commit]), [verdict], "l")


@pytest.mark.parametrize(
    ("labels", "value", "detail"),
    [
        ((), 0.0, "no fix keyword in the subject"),
        (("enhancement",), 0.0, "no fix keyword in the subject or labels"),
        (("C-bug",), 1.0, "label 'C-bug'"),
        (("kind/regression",), 1.0, "label 'kind/regression'"),
        (("debug",), 0.0, "no fix keyword in the subject or labels"),
    ],
)
def test_fix_keyword_reads_labels(labels: tuple[str, ...], value: float, detail: str) -> None:
    assert fix_keyword("Tidy the parser", labels) == (value, detail)


@pytest.mark.parametrize(
    ("title", "body", "detail"),
    [
        ("Closes #12: handle empty keys", None, "#12"),
        (
            "Handle empty keys",
            "Closes https://github.com/o/r/pull/2",
            "https://github.com/o/r/pull/2",
        ),
        ("Handle empty keys", "Fixes: #", "pull request #7, no closing keyword"),
    ],
)
def test_a_pull_requests_score_agrees_with_its_exported_linked_issues(
    title: str, body: str | None, detail: str
) -> None:
    # A closing keyword in the title, or before a pull-request URL, links no
    # issue (GitHub reads the description and the commit messages), so the
    # score must not give the 1.0 the export cannot back: 0.5 for the bare
    # reference at most.
    commit = pull_commit(pull(title=title, body=body), [COMMITS[1]], FILES, "o/r")
    (candidate,) = mine([commit]).candidates
    (feature,) = [f for f in candidate.features if f.name == "linked_reference"]
    assert (feature.value, feature.detail) == (0.5, detail)
    assert candidate_to_json(candidate, 1, "o/r")["pull_request"]["linked_issues"] == []


def test_a_pull_request_is_a_reference_of_its_own() -> None:
    info = pull_commit(pull(body=None), [], [], "o/r").pull_request
    assert linked_reference("Tidy", info) == (0.5, "pull request #7, no closing keyword")
    assert linked_reference("Tidy, see #2", info) == (0.5, "#2")
    assert linked_reference("Tidy", None) == (0.0, "no issue or pull-request reference")


# --- the walker over replayed fixtures -----------------------------------------------

LIST = "GET /repos/o/r/pulls?direction=desc&per_page=100&sort=updated&state=closed"


def serve(directory: Path, number: int, files: list[Any], commits: list[Any]) -> None:
    write_fixture(directory, f"GET /repos/o/r/pulls/{number}/files?per_page=100", reply(200, files))
    write_fixture(
        directory, f"GET /repos/o/r/pulls/{number}/commits?per_page=100", reply(200, commits)
    )


def test_merged_pulls_reads_merged_pulls_newest_merge_first(tmp_path: Path) -> None:
    link = f'<{API}/repositories/1/pulls?page=2>; rel="next"'
    first_page = [pull(1, "2024-01-01T00:00:00Z"), pull(2, None), pull(3, "2024-02-01T00:00:00Z")]
    write_fixture(tmp_path, LIST, reply(200, first_page, {"link": link}))
    write_fixture(tmp_path, "GET /repositories/1/pulls?page=2", reply(200, [pull(4, None)]))
    serve(tmp_path, 1, FILES, COMMITS)
    serve(tmp_path, 3, FILES[:1], [])
    with GitHubClient(transport=ReplayTransport(tmp_path), clock=FakeClock()) as client:
        walked = merged_pulls(client, "o/r", limit=10)
        assert client.stats.requests == 6
    assert [c.pull_request.number for c in walked.commits if c.pull_request] == [3, 1]
    assert walked.closed_unmerged == 2
    assert walked.too_many_files == ()
    assert isinstance(mine(walked.commits).candidates[0], Candidate)


def test_a_pull_request_repeated_on_the_next_page_is_read_once(tmp_path: Path) -> None:
    # A pull request further down was updated between the two list requests, so every
    # item moved one place and #3, the last of page 1, is also the first of page 2.
    link = f'<{API}/repositories/1/pulls?page=2>; rel="next"'
    first_page = [pull(1, "2024-01-01T00:00:00Z"), pull(3, "2024-02-01T00:00:00Z")]
    second_page = [pull(3, "2024-02-01T00:00:00Z"), pull(4, "2024-02-02T00:00:00Z")]
    write_fixture(tmp_path, LIST, reply(200, first_page, {"link": link}))
    write_fixture(tmp_path, "GET /repositories/1/pulls?page=2", reply(200, second_page))
    for number in (1, 3, 4):
        serve(tmp_path, number, FILES, COMMITS)
    with GitHubClient(transport=ReplayTransport(tmp_path), clock=FakeClock()) as client:
        walked = merged_pulls(client, "o/r", limit=10)
        assert client.stats.requests == 8
    assert [c.pull_request.number for c in walked.commits if c.pull_request] == [4, 3, 1]
    assert len(mine(walked.commits).candidates) == 3


def test_a_pull_request_without_a_number_is_an_error(tmp_path: Path) -> None:
    write_fixture(tmp_path, LIST, reply(200, [{**pull(1), "number": None}]))
    client = GitHubClient(transport=ReplayTransport(tmp_path), clock=FakeClock())
    with pytest.raises(GitHubError, match=r"pull.number: expected a count, got None"):
        merged_pulls(client, "o/r", limit=10)


def test_merged_pulls_stops_at_the_limit_and_skips_huge_pulls(tmp_path: Path) -> None:
    write_fixture(tmp_path, LIST, reply(200, [pull(1), pull(2), pull(3)]))
    many = [
        entry(filename=f"f{i}.py", additions=0, deletions=0, status="renamed") for i in range(3)
    ]
    serve(tmp_path, 1, many, COMMITS)
    link = f'<{API}/repositories/1/pulls/2/files?page=2>; rel="next"'
    write_fixture(
        tmp_path, "GET /repos/o/r/pulls/2/files?per_page=100", reply(200, many[:2], {"link": link})
    )
    with GitHubClient(transport=ReplayTransport(tmp_path), clock=FakeClock()) as client:
        walked = merged_pulls(client, "o/r", limit=2, max_files=2)
        assert client.stats.requests == 3
    assert walked.commits == ()
    assert walked.too_many_files == (1, 2)


def test_merged_pulls_rejects_bad_arguments_and_answers(tmp_path: Path) -> None:
    write_fixture(tmp_path, LIST, reply(200, [pull(1)]))
    write_fixture(tmp_path, "GET /repos/o/r/pulls/1/files?per_page=100", reply(200, {"a": 1}))
    with GitHubClient(transport=ReplayTransport(tmp_path), clock=FakeClock()) as client:
        with pytest.raises(GitHubError, match="not OWNER/REPO"):
            merged_pulls(client, "o", 1)
        with pytest.raises(ValueError, match="at least 1"):
            merged_pulls(client, "o/r", 0)
        with pytest.raises(GitHubError, match=r"files.*expected a list"):
            merged_pulls(client, "o/r", 1)


def test_merged_pulls_resume_after_a_watermark(tmp_path: Path) -> None:
    """A batch run lists only pull requests updated after its watermark, skipping walked ones."""
    listing = [
        pull(5, updated_at="2024-05-05T00:00:00Z", merge_commit_sha="5" * 40),
        pull(4, updated_at="2024-05-04T00:00:00Z", merge_commit_sha="4" * 40),
        pull(3, None, updated_at="2024-05-03T00:00:00Z"),
        pull(2, updated_at="2024-05-02T00:00:00Z", merge_commit_sha="2" * 40),
        pull(1, updated_at="2024-05-01T00:00:00Z", merge_commit_sha="1" * 40),
    ]
    write_fixture(tmp_path, LIST, reply(200, listing))
    for number in (1, 2, 4, 5):
        serve(tmp_path, number, FILES, COMMITS)
    with GitHubClient(transport=ReplayTransport(tmp_path), clock=FakeClock()) as client:
        first = merged_pulls(client, "o/r", limit=10)
        assert (first.newest, first.already_walked, len(first.commits)) == (
            "2024-05-05T00:00:00Z",
            0,
            4,
        )
        # Pull request 4 was walked before (updated since, say by a comment): not read again.
        resumed = merged_pulls(
            client, "o/r", limit=10, since="2024-05-02T00:00:00Z", seen={"4" * 40}
        )
    assert [c.pull_request.number for c in resumed.commits if c.pull_request] == [5]
    assert (resumed.already_walked, resumed.closed_unmerged) == (1, 1)
    assert resumed.newest == "2024-05-05T00:00:00Z"


def test_pull_sha_is_the_merge_commit_else_the_head() -> None:
    assert pull_sha(pull()) == MERGE
    assert pull_sha(pull(merge_commit_sha=None)) == HEAD
    assert pull_sha(pull(merge_commit_sha="")) == HEAD
    assert pull_sha({"head": None}) is None
    assert pull_sha({"head": {"sha": 7}}) is None
