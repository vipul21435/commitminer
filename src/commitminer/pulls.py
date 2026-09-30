"""Merged pull requests from the GitHub REST API, as commits the scorer can rank.

For each merged pull request (newest updated first) the walker reads the
pull request itself, its commits and its changed files: three endpoints,
at least 1 + 2 requests per pull request::

    GET /repos/{owner}/{repo}/pulls?state=closed&sort=updated&direction=desc
    GET /repos/{owner}/{repo}/pulls/{number}/commits
    GET /repos/{owner}/{repo}/pulls/{number}/files

Each one becomes a :class:`~commitminer.models.Commit` with a
:class:`~commitminer.models.PullRequest` attached, so the same filters,
score, difficulty estimate, fingerprints and ledger apply. Files are
classified by path only (their contents are not fetched), and each file's
patch, which GitHub sends with three context lines, is split into
``--unified=0`` hunks and measured like a walked commit's. A file whose patch
GitHub leaves out (binary, or too large) has no patch measurements.

Linked issues come from GitHub's closing keywords (close, closes, closed,
fix, fixes, fixed, resolve, resolves, resolved) followed by ``#123``,
``owner/repo#123`` or an issue URL, in the description or a commit message.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final

from commitminer.github import GitHubClient, GitHubError
from commitminer.gitlog import normalize_date
from commitminer.languages import language_of
from commitminer.models import Commit, FileChange, PullRequest
from commitminer.patch import FilePatch, PatchError, analyze, hunks_from_unified

REPO_NAME: Final = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]*)/[A-Za-z0-9._-]+$")
"""``OWNER/REPO`` as GitHub spells repository names."""

_CLOSING = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"(?:(?P<repo>[\w.-]+/[\w.-]+)?#(?P<number>\d+)\b"
    r"|https?://github\.com/(?P<url_repo>[\w.-]+/[\w.-]+)/issues/(?P<url_number>\d+)\b)",
    re.IGNORECASE,
)


def linked_issues(texts: Iterable[str], repo: str) -> tuple[str, ...]:
    """Issues closed by closing keywords in ``texts``: ``#12``, or ``owner/repo#12`` elsewhere.

    Sorted: this repository's issues first, by number, then other repositories'.
    """
    found: set[tuple[str, int]] = set()
    for text in texts:
        for match in _CLOSING.finditer(text):
            other = match.group("repo") or match.group("url_repo")
            number = int(match.group("number") or match.group("url_number"))
            same = other is None or other.lower() == repo.lower()
            found.add(("" if same else other or "", number))
    return tuple(f"{where}#{number}" for where, number in sorted(found))


def _string(value: Any, where: str) -> str:
    if not isinstance(value, str):
        raise GitHubError(f"{where}: expected a string, got {type(value).__name__}")
    return value


def _count(value: Any, where: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise GitHubError(f"{where}: expected a count, got {value!r}")
    return value


def _object(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GitHubError(f"{where}: expected an object, got {type(value).__name__}")
    return value


_NO_CONTENT_CHANGE: Final = frozenset({"renamed", "copied", "changed", "unchanged"})
"""File statuses that, with no line counts and no patch, mean a rename, copy or mode change."""


def file_change(item: Any, where: str = "file") -> FileChange:
    """One entry of ``GET .../pulls/{number}/files`` as a measured :class:`FileChange`.

    Without a ``patch``: a rename, copy or mode change with no line changes
    is an empty patch, a file with line counts has no measurements (GitHub
    leaves out large patches), and any other file is taken as binary. A patch whose
    lines do not add up to the file's counts (a truncated patch) is not
    measured either.
    """
    entry = _object(item, where)
    path = _string(entry.get("filename"), f"{where}.filename")
    previous = entry.get("previous_filename")
    old_path = None if previous is None else _string(previous, f"{where}.previous_filename")
    added = _count(entry.get("additions"), f"{where}.additions")
    deleted = _count(entry.get("deletions"), f"{where}.deletions")
    status = entry.get("status")
    text = entry.get("patch")
    if text is None:
        if added == deleted == 0:
            if status in _NO_CONTENT_CHANGE:
                empty = analyze(FilePatch(()), language_of(path))
                return FileChange(path, 0, 0, old_path, patch=empty)
            return FileChange(path, None, None, old_path)
        return FileChange(path, added, deleted, old_path)
    try:
        patch = FilePatch(hunks_from_unified(_string(text, f"{where}.patch")))
    except PatchError as exc:
        raise GitHubError(f"{where} ({path}): {exc}") from exc
    if (patch.added, patch.deleted) != (added, deleted):
        return FileChange(path, added, deleted, old_path)
    return FileChange(path, added, deleted, old_path, patch=analyze(patch, language_of(path)))


def _labels(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise GitHubError(f"{where}: expected a list")
    names = {_string(_object(item, where).get("name"), f"{where}.name") for item in value}
    return tuple(sorted(names))


def pull_commit(
    item: Any, commits: list[Any], files: list[Any], repo: str, where: str = "pull"
) -> Commit:
    """A merged pull request, its commits and its files as one :class:`Commit`."""
    pull = _object(item, where)
    number = _count(pull.get("number"), f"{where}.number")
    at = f"pull #{number}"
    merged_at = _string(pull.get("merged_at"), f"{at}.merged_at")
    base = _object(pull.get("base"), f"{at}.base")
    head = _object(pull.get("head"), f"{at}.head")
    merge_sha = pull.get("merge_commit_sha")
    if merge_sha is not None:
        merge_sha = _string(merge_sha, f"{at}.merge_commit_sha")
    title = _string(pull.get("title"), f"{at}.title")
    body = pull.get("body") or ""
    body = _string(body, f"{at}.body").replace("\r\n", "\n").strip()
    shas: list[str] = []
    messages = [body]
    for index, raw in enumerate(commits):
        entry = _object(raw, f"{at}.commits[{index}]")
        shas.append(_string(entry.get("sha"), f"{at}.commits[{index}].sha"))
        detail = _object(entry.get("commit"), f"{at}.commits[{index}].commit")
        messages.append(_string(detail.get("message"), f"{at}.commits[{index}].message"))
    head_sha = _string(head.get("sha"), f"{at}.head.sha")
    info = PullRequest(
        number=number,
        url=_string(pull.get("html_url"), f"{at}.html_url"),
        title=title,
        merged_at=normalize_date(merged_at),
        base_ref=_string(base.get("ref"), f"{at}.base.ref"),
        base_sha=_string(base.get("sha"), f"{at}.base.sha"),
        head_sha=head_sha,
        merge_commit_sha=merge_sha,
        labels=_labels(pull.get("labels"), f"{at}.labels"),
        linked_issues=linked_issues(messages, repo),
        commits=tuple(shas),
    )
    return Commit(
        sha=info.merge_commit_sha or head_sha,
        parents=(info.base_sha,),
        date=info.merged_at,
        message=f"{title}\n\n{body}\n" if body else f"{title}\n",
        files=tuple(file_change(f, f"{at}.files[{i}]") for i, f in enumerate(files)),
        pull_request=info,
    )


@dataclass(frozen=True, slots=True)
class PullWalk:
    """The merged pull requests read (newest merge first) and the ones passed over.

    ``too_many_files`` lists the merged pull requests with more than the
    allowed number of changed files: their files were not read in full, so
    they are not ranked (they could not be small, focused tasks anyway).
    """

    commits: tuple[Commit, ...]
    closed_unmerged: int
    too_many_files: tuple[int, ...] = ()


DEFAULT_MAX_FILES: Final = 300
"""Changed files beyond which a pull request's file list is not read further (3 pages)."""


def _files(client: GitHubClient, url: str, max_files: int) -> list[Any] | None:
    """The changed files of a pull request, or ``None`` when there are more than ``max_files``."""
    files: list[Any] = []
    for page in client.pages(url, {"per_page": 100}):
        if not isinstance(page.data, list):
            raise GitHubError(f"GET {url}: expected a list, got {type(page.data).__name__}")
        files.extend(page.data)
        if len(files) > max_files or (len(files) == max_files and page.next_url is not None):
            return None
    return files


def merged_pulls(
    client: GitHubClient, repo: str, limit: int, max_files: int = DEFAULT_MAX_FILES
) -> PullWalk:
    """Read the ``limit`` most recently updated merged pull requests of ``repo`` (``OWNER/REPO``).

    Files are read before commits, so a pull request with more than
    ``max_files`` changed files costs at most ``max_files / 100`` requests.
    """
    if not REPO_NAME.match(repo):
        raise GitHubError(f"{repo!r} is not OWNER/REPO")
    if limit < 1 or max_files < 1:
        raise ValueError("limit and max_files must be at least 1")
    merged: list[dict[str, Any]] = []
    unmerged = 0
    params: dict[str, str | int] = {
        "state": "closed",
        "sort": "updated",
        "direction": "desc",
        "per_page": 100,
    }
    for item in client.items(f"/repos/{repo}/pulls", params):
        pull = _object(item, "pull")
        if pull.get("merged_at") is None:
            unmerged += 1
            continue
        merged.append(pull)
        if len(merged) == limit:
            break
    commits: list[Commit] = []
    skipped: list[int] = []
    for pull in merged:
        number = _count(pull.get("number"), "pull.number")
        base = f"/repos/{repo}/pulls/{number}"
        pull_files = _files(client, f"{base}/files", max_files)
        if pull_files is None:
            skipped.append(number)
            continue
        pull_commits = list(client.items(f"{base}/commits", {"per_page": 100}))
        commits.append(pull_commit(pull, pull_commits, pull_files, repo))
    commits.sort(key=lambda c: (c.date, c.pull_request.number if c.pull_request else 0))
    return PullWalk(tuple(reversed(commits)), unmerged, tuple(sorted(skipped)))
