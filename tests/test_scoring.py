from __future__ import annotations

import pytest

from commitminer.classify import Category
from commitminer.models import Commit, FileChange
from commitminer.scoring import (
    Candidate,
    Rejection,
    RejectReason,
    Settings,
    Weights,
    evaluate,
    fix_keyword,
    linked_reference,
    mine,
)

SRC = FileChange("src/pkg/parser.py", 10, 2)
TEST = FileChange("tests/test_parser.py", 20, 0)


def make(
    message: str = "Change",
    files: tuple[FileChange, ...] = (SRC, TEST),
    sha: str = "a" * 40,
    date: str = "2024-01-01T00:00:00+00:00",
) -> Commit:
    return Commit(sha, ("0" * 40,), date, message, files)


def as_candidate(outcome: Candidate | Rejection) -> Candidate:
    assert isinstance(outcome, Candidate), outcome
    return outcome


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ((), RejectReason.EMPTY),
        ((FileChange("README.md", 5, 1), TEST), RejectReason.NO_SOURCE),
        ((SRC, FileChange("CHANGELOG.md", 3, 0)), RejectReason.NO_TEST),
        ((FileChange("src/pkg/a.py", 300, 1), FileChange("tests/t.py", 100, 0)), "too-large"),
    ],
)
def test_filter_reasons(files: tuple[FileChange, ...], reason: str) -> None:
    outcome = evaluate(make(files=files), Settings())
    assert isinstance(outcome, Rejection)
    assert outcome.reason == reason


def test_golden_score_breakdown() -> None:
    commit = make(
        "Fix crash on empty table\n\nfixes #12",
        (SRC, TEST, FileChange("README.md", 100, 0)),
    )
    candidate = as_candidate(evaluate(commit, Settings()))
    table = [(f.name, f.value, f.weight, f.contribution, f.detail) for f in candidate.features]
    assert table == [
        ("small_diff", 0.92, 3.0, 2.76, "32 of at most 400 source+test lines changed"),
        ("test_lines_added", 0.5, 3.0, 1.5, "20 test lines added (full value at 40)"),
        ("linked_reference", 1.0, 2.0, 2.0, "fixes #12"),
        ("fix_keyword", 1.0, 1.0, 1.0, "Fix"),
        ("focused_source", 1.0, 1.0, 1.0, "1 source file changed"),
    ]
    assert candidate.score == 8.26
    assert candidate.stats.changed_lines == 32
    assert candidate.stats.added(Category.TEST) == 20
    assert candidate.stats.deleted(Category.SOURCE) == 2


def test_size_limit_is_inclusive_and_values_stay_in_range() -> None:
    files = (FileChange("src/a.py", 350, 0), FileChange("src/b.py", 0, 0), TEST, TEST)
    candidate = as_candidate(evaluate(make(files=files), Settings()))
    values = {f.name: f.value for f in candidate.features}
    assert candidate.stats.changed_lines == 390
    assert values["small_diff"] == 0.025
    assert values["test_lines_added"] == 1.0
    assert values["focused_source"] == 0.5
    assert all(0.0 <= v <= 1.0 for v in values.values())


def test_custom_weights_and_limits() -> None:
    settings = Settings(max_lines=40, test_lines_cap=10, weights=Weights(1, 1, 1, 1, 1))
    candidate = as_candidate(evaluate(make(), settings))
    assert [f.contribution for f in candidate.features] == [0.2, 1.0, 0.0, 0.0, 1.0]
    assert candidate.score == 2.2


def test_settings_validation() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        Settings(max_lines=0)


def test_renames_are_classified_by_new_path_and_binary_tests_count() -> None:
    files = (
        FileChange("src/pkg/new.py", 1, 1, old_path="scripts/old.py"),
        FileChange("tests/data/case.bin", None, None),
    )
    candidate = as_candidate(evaluate(make(files=files), Settings()))
    assert [f.change.path for f in candidate.stats.source_files] == ["src/pkg/new.py"]
    assert candidate.stats.changed_lines == 2


@pytest.mark.parametrize(
    ("message", "value", "detail"),
    [
        ("Parse dates (#123)", 0.5, "#123"),
        ("Closes owner/repo#7", 1.0, "Closes owner/repo#7"),
        ("Resolved: #4", 1.0, "Resolved: #4"),
        ("fix gh-55 in the lexer", 0.5, "gh-55"),
        (
            "See https://github.com/o/r/issues/9",
            0.5,
            "https://github.com/o/r/issues/9",
        ),
        ("Fixes https://github.com/o/r/pull/10", 1.0, "Fixes https://github.com/o/r/pull/10"),
        ("Escape &#39; and use abc#12 as a key", 0.0, "no issue or pull-request reference"),
        ("Plain change", 0.0, "no issue or pull-request reference"),
    ],
)
def test_linked_reference(message: str, value: float, detail: str) -> None:
    assert linked_reference(message) == (value, detail)


@pytest.mark.parametrize(
    ("subject", "value"),
    [
        ("FIX: three odd cases", 1.0),
        ("Bugfix for dotted keys", 1.0),
        ("Handle regression in 2.0", 1.0),
        ("Parser crashed on BOM", 1.0),
        ("Improve prefix handling", 0.0),
        ("Add a new API", 0.0),
    ],
)
def test_fix_keyword(subject: str, value: float) -> None:
    assert fix_keyword(subject)[0] == value


def test_mine_ranks_by_score_then_newest_then_sha() -> None:
    # Dates compare in UTC: 01:30+02:00 is 23:30 UTC on the previous day.
    midnight = make("Change", sha="b" * 40, date="2024-01-01T00:00:00+00:00")
    earlier_d = make("Change", sha="d" * 40, date="2024-01-01T01:30:00+02:00")
    earlier_c = make("Change", sha="c" * 40, date="2024-01-01T01:30:00+02:00")
    best = make("Fix bug (#1)", sha="e" * 40)
    rejected = [make(files=()), make(files=(SRC,)), make(files=(TEST,)), make(files=(SRC,))]
    result = mine([earlier_d, midnight, earlier_c, best, *rejected])
    assert result.walked == 8
    assert [c.commit.sha[0] for c in result.candidates] == ["e", "b", "c", "d"]
    assert result.rejected_by_reason() == {"empty": 1, "no-source": 1, "no-test": 2}


def test_mine_with_no_commits() -> None:
    result = mine([])
    assert result.walked == 0
    assert result.candidates == ()
    assert result.rejected_by_reason() == {}
