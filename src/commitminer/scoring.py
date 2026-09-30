"""Soft scoring and a difficulty estimate for the commits that pass the hard filters.

Both are transparent linear models, ``total = sum(weight * value)`` over
features whose values lie in ``[0, 1]``; every feature keeps its value, weight,
contribution and a readable detail, so a ranking can always be explained line
by line and recomputed by hand.

- The **score** says how promising a commit is as a fail-to-pass task: a small
  diff, tests that grew and assert something, a linked issue, a fix keyword, one
  source file. Candidates are ranked by it.
- The **difficulty** says how much work the fix is for whoever solves the task:
  files touched, source hunks and code lines, edits spread over several source
  files, public API changed. It is reported as a value and an easy, medium or
  hard band; it does not change the ranking.

The hard filters that decide which commits are candidates live in
:mod:`commitminer.filters`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from commitminer.classify import Category
from commitminer.filters import RejectReason, check
from commitminer.fingerprint import Fingerprint, fingerprint
from commitminer.models import Commit, PullRequest
from commitminer.settings import Settings
from commitminer.stats import DiffStats, diff_stats

NO_PATCH = "unknown: no patch data"


@dataclass(frozen=True, slots=True)
class Feature:
    """One feature of a linear model: ``contribution = weight * value``."""

    name: str
    value: float
    weight: float
    contribution: float
    detail: str


@dataclass(frozen=True, slots=True)
class Difficulty:
    """The difficulty estimate of one candidate."""

    features: tuple[Feature, ...]
    value: float
    band: str


@dataclass(frozen=True, slots=True)
class Candidate:
    """A commit that passed the filter, with its score and difficulty breakdowns.

    ``fingerprint`` identifies the fix for dedupe (``None`` without hunk hashes).
    """

    commit: Commit
    stats: DiffStats
    features: tuple[Feature, ...]
    score: float
    difficulty: Difficulty
    fingerprint: Fingerprint | None = None


@dataclass(frozen=True, slots=True)
class Rejection:
    """A commit that the filter dropped, and why."""

    commit: Commit
    reason: RejectReason
    stats: DiffStats


_CLOSING_REF = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b:?\s+"
    r"((?:[\w.-]+/[\w.-]+)?#\d+|https://github\.com/\S+/(?:issues|pull)/\d+)",
    re.IGNORECASE,
)
_BARE_REF = re.compile(
    r"(?<![\w/&])(?:[\w.-]+/[\w.-]+)?#\d+\b"
    r"|\bgh-\d+\b"
    r"|https://github\.com/\S+/(?:issues|pull)/\d+",
    re.IGNORECASE,
)
_FIX_WORD = re.compile(
    r"\b(fix(?:e[sd]|ing)?|bug(?:s|fix)?|regression|crash(?:es|ed)?|incorrect(?:ly)?"
    r"|wrong|broken)\b",
    re.IGNORECASE,
)
_FIX_LABEL = re.compile(r"\b(?:bugs?|bugfix|fix(?:es)?|regression|crash(?:es)?)\b", re.IGNORECASE)
"""Label names that mark a bug fix: ``bug``, ``type: bug``, ``C-bug``, ``regression`` ..."""


def linked_reference(message: str, pull: PullRequest | None = None) -> tuple[float, str]:
    """1.0 for a closing keyword with a reference, 0.5 for a bare reference, else 0.0.

    For a pull request, only the issues it closes (from its description and its
    commit messages, as :func:`commitminer.pulls.linked_issues` reads them) count
    as closing references, so the score agrees with the exported
    ``linked_issues``: a closing keyword in the title, or one before a
    pull-request URL, is a bare reference at most. The pull request itself
    counts as a bare reference: a squash-merged commit that ends in ``(#123)``
    gets 0.5 for pointing at the same discussion.
    """
    if pull is not None:
        if pull.linked_issues:
            return 1.0, f"pull request #{pull.number} closes {', '.join(pull.linked_issues)}"
    elif (closing := _CLOSING_REF.search(message)) is not None:
        return 1.0, closing.group(0)
    bare = _BARE_REF.search(message)
    if bare is not None:
        return 0.5, bare.group(0)
    if pull is not None:
        return 0.5, f"pull request #{pull.number}, no closing keyword"
    return 0.0, "no issue or pull-request reference"


def fix_keyword(subject: str, labels: tuple[str, ...] = ()) -> tuple[float, str]:
    """1.0 when the subject line reads like a bug fix, or a pull-request label says so."""
    match = _FIX_WORD.search(subject)
    if match is not None:
        return 1.0, match.group(0)
    for label in labels:
        if _FIX_LABEL.search(label):
            return 1.0, f"label {label!r}"
    return 0.0, "no fix keyword in the subject" + (" or labels" if labels else "")


def _feature(name: str, value: float, weight: float, detail: str) -> Feature:
    value = round(value, 4)
    return Feature(name, value, weight, round(weight * value, 4), detail)


def _capped(count: int, cap: int) -> float:
    return min(count, cap) / cap


def _plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def features(commit: Commit, stats: DiffStats, settings: Settings) -> tuple[Feature, ...]:
    """The score features of a commit that passed :func:`~commitminer.filters.check`."""
    weights = settings.weights
    size = stats.changed_lines
    test_added = stats.added(Category.TEST)
    inline = len(stats.inline_test_files)
    test_detail = f"{test_added} test lines added (full value at {settings.test_lines_cap})"
    if inline:
        test_detail += f", in #[cfg(test)] of {_plural(inline, 'src file')}"
    asserts = stats.assertions
    assert_detail = (
        NO_PATCH
        if asserts is None
        else f"{_plural(asserts, 'assertion line')} added in tests "
        f"(full value at {settings.assertions_cap})"
    )
    sources = len(stats.source_files)
    pull = commit.pull_request
    ref_value, ref_detail = linked_reference(commit.message, pull)
    fix_value, fix_detail = fix_keyword(commit.subject, pull.labels if pull else ())
    return (
        _feature(
            "small_diff",
            1 - _capped(size, settings.max_lines),
            weights.small_diff,
            f"{size} of at most {settings.max_lines} source+test lines changed",
        ),
        _feature(
            "test_lines_added",
            _capped(test_added, settings.test_lines_cap),
            weights.test_lines_added,
            test_detail,
        ),
        _feature(
            "added_assertions",
            _capped(asserts or 0, settings.assertions_cap),
            weights.added_assertions,
            assert_detail,
        ),
        _feature("linked_reference", ref_value, weights.linked_reference, ref_detail),
        _feature("fix_keyword", fix_value, weights.fix_keyword, fix_detail),
        _feature(
            "focused_source",
            1 / sources,
            weights.focused_source,
            f"{_plural(sources, 'source file')} changed",
        ),
    )


def _names(names: tuple[str, ...], shown: int = 3) -> str:
    text = ", ".join(names[:shown])
    return text + (f" (+{len(names) - shown} more)" if len(names) > shown else "")


def difficulty(stats: DiffStats, settings: Settings) -> Difficulty:
    """The difficulty features of a candidate, their sum and its band."""
    weights = settings.difficulty_weights
    files = len(stats.of(Category.SOURCE, Category.TEST))
    hunks = stats.code_hunks
    lines = stats.code_lines
    spread = max(len(stats.code_files) - 1, 0)
    api = stats.public_api
    feats = (
        _feature(
            "files",
            _capped(files, settings.files_cap),
            weights.files,
            f"{_plural(files, 'source+test file')} changed (full value at {settings.files_cap})",
        ),
        _feature(
            "hunks",
            _capped(hunks or 0, settings.hunks_cap),
            weights.hunks,
            NO_PATCH
            if hunks is None
            else f"{_plural(hunks, 'source code hunk')} (full value at {settings.hunks_cap})",
        ),
        _feature(
            "lines",
            _capped(lines or 0, settings.lines_cap),
            weights.lines,
            NO_PATCH
            if lines is None
            else f"{_plural(lines, 'source code line')} changed "
            f"(full value at {settings.lines_cap})",
        ),
        _feature(
            "cross_file",
            _capped(spread, settings.cross_file_cap),
            weights.cross_file,
            f"{_plural(len(stats.code_files), 'source file')} with code changes "
            f"(full value at {settings.cross_file_cap + 1})",
        ),
        _feature(
            "public_api",
            1.0 if api else 0.0,
            weights.public_api,
            NO_PATCH if api is None else _names(api) if api else "no public declaration changed",
        ),
    )
    value = round(sum(f.contribution for f in feats), 4)
    return Difficulty(feats, value, settings.band(value))


def evaluate(commit: Commit, settings: Settings) -> Candidate | Rejection:
    """Filter one commit and, if it passes, score it and estimate its difficulty."""
    stats = diff_stats(commit, settings.rules)
    reason = check(stats, settings)
    if reason is not None:
        return Rejection(commit, reason, stats)
    feats = features(commit, stats, settings)
    score = round(sum(f.contribution for f in feats), 4)
    return Candidate(commit, stats, feats, score, difficulty(stats, settings), fingerprint(stats))


def _rank_key(candidate: Candidate) -> tuple[float, float, str]:
    timestamp = datetime.fromisoformat(candidate.commit.date).timestamp()
    return (-candidate.score, -timestamp, candidate.commit.sha)


@dataclass(frozen=True, slots=True)
class MineResult:
    """Ranked candidates (best first) and the rejected commits."""

    walked: int
    candidates: tuple[Candidate, ...]
    rejections: tuple[Rejection, ...]

    def rejected_by_reason(self) -> dict[str, int]:
        """Rejection counts per reason, in the order the filter checks them."""
        counts = Counter(r.reason for r in self.rejections)
        return {reason.value: counts[reason] for reason in RejectReason if counts[reason]}

    def bands(self) -> dict[str, int]:
        """Candidate counts per difficulty band, easy to hard, bands with none left out."""
        counts = Counter(c.difficulty.band for c in self.candidates)
        return {band: counts[band] for band in ("easy", "medium", "hard") if counts[band]}


def mine(commits: Iterable[Commit], settings: Settings | None = None) -> MineResult:
    """Filter and score commits; rank by score, then newest first, then sha."""
    settings = settings or Settings()
    candidates: list[Candidate] = []
    rejections: list[Rejection] = []
    walked = 0
    for commit in commits:
        walked += 1
        outcome = evaluate(commit, settings)
        if isinstance(outcome, Candidate):
            candidates.append(outcome)
        else:
            rejections.append(outcome)
    candidates.sort(key=_rank_key)
    return MineResult(walked, tuple(candidates), tuple(rejections))
