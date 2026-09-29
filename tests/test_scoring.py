from __future__ import annotations

import re
from collections.abc import Callable

import pytest

from commitminer.classify import RULES, Category, Rule
from commitminer.filters import RejectReason, oversize_detail
from commitminer.models import Commit, FileChange, PatchStats
from commitminer.scoring import (
    Candidate,
    Rejection,
    evaluate,
    fix_keyword,
    linked_reference,
    mine,
)
from commitminer.settings import DifficultyWeights, Settings, Weights

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


def patched(path: str, added: int, deleted: int, **stats: object) -> FileChange:
    """A file change with patch measurements; code counts default to the raw ones."""
    fields: dict[str, object] = {
        "hunks": 1,
        "code_hunks": 1,
        "code_added": added,
        "code_deleted": deleted,
    }
    fields.update(stats)
    return FileChange(path, added, deleted, patch=PatchStats(**fields))  # type: ignore[arg-type]


COMMENTS_ONLY = patched("src/pkg/a.py", 3, 3, hunks=3, code_hunks=0, code_added=0, code_deleted=0)
TESTS_ONLY_RS = FileChange(
    "src/lib.rs",
    8,
    1,
    signals=("rust-inline-tests", "rust-tests-added"),
    patch=PatchStats(2, 0, 0, 0, test_added=8, test_deleted=1, asserts=1),
)
MANY_SOURCES = tuple(FileChange(f"src/pkg/m{n}.py", 1, 0) for n in range(11))


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ((), RejectReason.EMPTY),
        ((FileChange("README.md", 5, 1), FileChange("docs/a.rst", 1, 0)), "docs-only"),
        ((FileChange("go.sum", 5, 1), FileChange("vendor/x/y.go", 1, 0)), "generated-only"),
        ((FileChange("README.md", 5, 1), TEST), RejectReason.NO_SOURCE),
        ((FileChange("README.md", 5, 1), FileChange("pyproject.toml", 1, 1)), "no-source"),
        ((FileChange("src/pkg/a.py", 0, 0, old_path="pkg/a.py"), TEST), "source-unchanged"),
        ((FileChange("src/pkg/_ext.pyx", None, None), TEST), "source-unchanged"),
        ((TESTS_ONLY_RS,), "source-unchanged"),
        ((COMMENTS_ONLY, TEST), "source-cosmetic"),
        ((SRC, FileChange("CHANGELOG.md", 3, 0)), RejectReason.NO_TEST),
        ((FileChange("src/pkg/a.py", 300, 1), FileChange("tests/t.py", 100, 0)), "oversize"),
        ((*MANY_SOURCES, TEST), "oversize"),
        ((FileChange("api/v1/svc.pb.go", 40, 3), FileChange("svc_test.go", 9, 0)), "no-source"),
        ((FileChange("vendor/x/y.go", 4, 1), FileChange("y_test.go", 9, 0)), "no-source"),
        ((FileChange("src/lib.rs", 4, 1), FileChange("vendor/x/tests/t.rs", 9, 0)), "no-test"),
    ],
)
def test_filter_reasons(files: tuple[FileChange, ...], reason: str) -> None:
    outcome = evaluate(make(files=files), Settings())
    assert isinstance(outcome, Rejection)
    assert outcome.reason == reason
    assert outcome.reason.description


def test_every_reason_has_a_description() -> None:
    assert all(reason.description for reason in RejectReason)


def test_oversize_detail_names_the_broken_limits() -> None:
    settings = Settings(max_lines=10, max_source_files=2)
    rejection = evaluate(make(files=(*MANY_SOURCES[:3], TEST)), settings)
    assert isinstance(rejection, Rejection)
    assert oversize_detail(rejection.stats, settings) == (
        "23 source+test lines (max 10), 3 source files (max 2)"
    )
    lines_only = Settings(max_lines=10)
    assert oversize_detail(rejection.stats, lines_only) == "23 source+test lines (max 10)"
    files_only = Settings(max_source_files=2)
    assert oversize_detail(rejection.stats, files_only) == "3 source files (max 2)"


def test_cosmetic_check_needs_patch_data_for_every_source_file() -> None:
    # One source file without patch data might hold the fix: keep the commit.
    candidate = as_candidate(evaluate(make(files=(COMMENTS_ONLY, SRC, TEST)), Settings()))
    assert [f.change.path for f in candidate.stats.code_files] == ["src/pkg/parser.py"]


def test_golden_score_breakdown_without_patch_data() -> None:
    commit = make(
        "Fix crash on empty table\n\nfixes #12",
        (SRC, TEST, FileChange("README.md", 100, 0)),
    )
    candidate = as_candidate(evaluate(commit, Settings()))
    table = [(f.name, f.value, f.weight, f.contribution, f.detail) for f in candidate.features]
    assert table == [
        ("small_diff", 0.92, 3.0, 2.76, "32 of at most 400 source+test lines changed"),
        ("test_lines_added", 0.5, 2.0, 1.0, "20 test lines added (full value at 40)"),
        ("added_assertions", 0.0, 1.0, 0.0, "unknown: no patch data"),
        ("linked_reference", 1.0, 2.0, 2.0, "fixes #12"),
        ("fix_keyword", 1.0, 1.0, 1.0, "Fix"),
        ("focused_source", 1.0, 1.0, 1.0, "1 source file changed"),
    ]
    assert candidate.score == 7.76
    assert candidate.stats.changed_lines == 32
    assert candidate.stats.added(Category.TEST) == 20
    assert candidate.stats.deleted(Category.SOURCE) == 2
    assert (candidate.stats.added(Category.DOCS), candidate.stats.deleted(Category.DOCS)) == (
        100,
        0,
    )
    level = candidate.difficulty
    assert [(f.name, f.contribution, f.detail) for f in level.features] == [
        ("files", 0.2, "2 source+test files changed (full value at 10)"),
        ("hunks", 0.0, "unknown: no patch data"),
        ("lines", 0.0, "unknown: no patch data"),
        ("cross_file", 0.0, "1 source file with code changes (full value at 5)"),
        ("public_api", 0.0, "unknown: no patch data"),
    ]
    assert (level.value, level.band) == (0.2, "easy")


def test_golden_score_and_difficulty_with_patch_data() -> None:
    commit = make(
        "Fix quoting in dump (#40)",
        (
            patched(
                "src/pkg/dump.py", 30, 6, hunks=5, code_hunks=4, code_added=24, api=("def dump",)
            ),
            patched("src/pkg/util.py", 4, 1, hunks=2, code_added=3, api=("class Quote",)),
            patched("src/pkg/notes.py", 2, 0, code_hunks=0, code_added=0),
            patched("tests/test_dump.py", 25, 0, hunks=2, asserts=7),
            patched("tests/data/quoted.toml", 6, 0),
        ),
    )
    candidate = as_candidate(evaluate(commit, Settings()))
    assert [(f.name, f.value, f.contribution, f.detail) for f in candidate.features] == [
        ("small_diff", 0.815, 2.445, "74 of at most 400 source+test lines changed"),
        ("test_lines_added", 0.775, 1.55, "31 test lines added (full value at 40)"),
        ("added_assertions", 1.0, 1.0, "7 assertion lines added in tests (full value at 5)"),
        ("linked_reference", 0.5, 1.0, "#40"),
        ("fix_keyword", 1.0, 1.0, "Fix"),
        ("focused_source", 0.3333, 0.3333, "3 source files changed"),
    ]
    assert candidate.score == 7.3283
    level = candidate.difficulty
    assert [(f.name, f.value, f.contribution, f.detail) for f in level.features] == [
        ("files", 0.5, 0.5, "5 source+test files changed (full value at 10)"),
        ("hunks", 0.5, 1.5, "5 source code hunks (full value at 10)"),
        ("lines", 0.34, 1.02, "34 source code lines changed (full value at 100)"),
        ("cross_file", 0.25, 0.5, "2 source files with code changes (full value at 5)"),
        ("public_api", 1.0, 1.0, "class Quote, def dump"),
    ]
    assert (level.value, level.band) == (4.52, "hard")


def test_difficulty_bands_and_long_api_lists() -> None:
    settings = Settings()
    assert [settings.band(v) for v in (0.0, 1.99, 2.0, 4.49, 4.5, 10.0)] == [
        "easy",
        "easy",
        "medium",
        "medium",
        "hard",
        "hard",
    ]
    assert settings.bands_label == "easy < 2 <= medium < 4.5 <= hard"
    api = ("def a", "def b", "def c", "def d", "def e")
    commit = make(files=(patched("src/pkg/api.py", 5, 0, api=api), TEST))
    detail = as_candidate(evaluate(commit, settings)).difficulty.features[-1].detail
    assert detail == "def a, def b, def c (+2 more)"
    plain = make(files=(patched("src/pkg/api.py", 5, 0), TEST))
    detail = as_candidate(evaluate(plain, settings)).difficulty.features[-1].detail
    assert detail == "no public declaration changed"


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
    settings = Settings(
        max_lines=40,
        test_lines_cap=10,
        weights=Weights(1, 1, 1, 1, 1, 1),
        difficulty_weights=DifficultyWeights(files=5, hunks=0, lines=0, cross_file=0),
        files_cap=4,
    )
    candidate = as_candidate(evaluate(make(), settings))
    assert [f.contribution for f in candidate.features] == [0.2, 1.0, 0.0, 0.0, 0.0, 1.0]
    assert candidate.score == 2.2
    assert candidate.difficulty.value == 2.5


@pytest.mark.parametrize(
    ("build", "message"),
    [
        (lambda: Settings(max_lines=0), "max_lines must be at least 1"),
        (lambda: Settings(hunks_cap=0), "hunks_cap must be at least 1"),
        (lambda: Settings(medium_at=5, hard_at=4), "0 <= medium_at <= hard_at"),
        (lambda: Settings(medium_at=-1), "0 <= medium_at <= hard_at"),
        (lambda: Settings(hard_at=float("inf")), "0 <= medium_at <= hard_at"),
        (lambda: Weights(small_diff=-1), "weight small_diff must be a finite number"),
        (lambda: DifficultyWeights(lines=float("nan")), "weight lines must be"),
    ],
)
def test_settings_validation(build: Callable[[], object], message: str) -> None:
    with pytest.raises(ValueError, match=re.escape(message)):
        build()


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


def test_generated_and_vendored_files_do_not_count_toward_size() -> None:
    commit = make(
        "Fix lexer on CRLF input",
        (
            FileChange("internal/lexer/lexer.go", 6, 2),
            FileChange("internal/lexer/lexer_test.go", 14, 0),
            FileChange("go.sum", 900, 20),
            FileChange("vendor/golang.org/x/text/width.go", 700, 0),
        ),
    )
    candidate = as_candidate(evaluate(commit, Settings(max_lines=40)))
    assert candidate.stats.changed_lines == 22
    categories = [f.category for f in candidate.stats.files]
    assert categories == [Category.SOURCE, Category.TEST, Category.GENERATED, Category.VENDORED]


def test_settings_carry_the_rule_table() -> None:
    fixtures = Rule("fixtures", Category.TEST, "dir", ("fixtures",), "project test data")
    commit = make(files=(SRC, FileChange("fixtures/case.json", 5, 0)))
    assert isinstance(evaluate(commit, Settings()), Rejection)
    candidate = as_candidate(evaluate(commit, Settings(rules=(fixtures, *RULES))))
    assert candidate.stats.test_files[0].classification.rule_id == "fixtures"


def test_rust_inline_tests_count_as_tests() -> None:
    inline = FileChange("src/lib.rs", 12, 1, signals=("rust-inline-tests", "rust-tests-added"))
    only_code = FileChange("src/lib.rs", 3, 1, signals=("rust-inline-tests",))
    rejected = evaluate(make(files=(only_code,)), Settings())
    assert isinstance(rejected, Rejection)
    assert rejected.reason is RejectReason.NO_TEST
    candidate = as_candidate(evaluate(make("Fix overflow in add", (inline,)), Settings()))
    assert candidate.stats.inline_test_files[0].classification.rule_id == "rust-inline-tests"
    assert candidate.stats.test_files == ()
    test_feature = candidate.features[1]
    assert test_feature.value == 0.0
    assert (
        test_feature.detail
        == "0 test lines added (full value at 40), in #[cfg(test)] of 1 src file"
    )
    two = (inline, FileChange("src/io.rs", 5, 0, signals=inline.signals))
    detail = as_candidate(evaluate(make(files=two), Settings())).features[1].detail
    assert detail.endswith("in #[cfg(test)] of 2 src files")


def test_inline_test_lines_move_from_source_to_test() -> None:
    fix = FileChange(
        "src/lib.rs",
        9,
        2,
        signals=("rust-inline-tests",),
        patch=PatchStats(3, 1, 1, 1, test_added=8, test_deleted=1, asserts=2),
    )
    candidate = as_candidate(evaluate(make("Fix overflow", (fix,)), Settings()))
    stats = candidate.stats
    # No new #[test] attribute, but test lines were added: the tests changed.
    assert [f.change.path for f in stats.inline_test_files] == ["src/lib.rs"]
    assert (stats.added(Category.SOURCE), stats.deleted(Category.SOURCE)) == (1, 1)
    assert (stats.added(Category.TEST), stats.deleted(Category.TEST)) == (8, 1)
    assert stats.changed_lines == 11
    assert stats.assertions == 2
    assert candidate.features[1].detail.startswith("8 test lines added")


def test_mine_counts_candidates_per_band() -> None:
    small = make("Fix", (patched("src/pkg/a.py", 2, 1), TEST), sha="a" * 40)
    big = make(
        "Fix",
        (patched("src/pkg/a.py", 120, 10, hunks=12, api=("def a",)), TEST),
        sha="b" * 40,
    )
    assert mine([small, big]).bands() == {"easy": 1, "hard": 1}


def test_generated_header_signal_moves_a_file_out_of_source() -> None:
    generated = FileChange("internal/kind/kind_string.go", 30, 5, signals=("generated-header",))
    outcome = evaluate(make(files=(generated, FileChange("kind_test.go", 5, 0))), Settings())
    assert isinstance(outcome, Rejection)
    assert outcome.reason is RejectReason.NO_SOURCE
