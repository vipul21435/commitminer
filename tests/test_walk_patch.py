"""The walker measures every file's --unified=0 patch (real synthetic repositories)."""

from __future__ import annotations

import pytest

from commitminer import gitlog
from commitminer.filters import RejectReason
from commitminer.gitlog import walk
from commitminer.models import PatchStats
from commitminer.scoring import Candidate, Rejection, evaluate
from commitminer.settings import Settings
from gitrepo import GitRepo, lines, unhashed
from test_blobs import LIB_V1, LIB_V2

CORE = "import re\n\n\ndef load(fp):\n    return fp.read()\n"


def test_walk_measures_code_comments_api_and_inline_tests(git_repo: GitRepo) -> None:
    git_repo.commit("base", {"src/pkg/core.py": CORE, "src/lib.rs": LIB_V1})
    pragma_core = CORE.replace("re\n", "re  # pragma: no cover\n")
    git_repo.commit("pragma", {"src/pkg/core.py": pragma_core})
    git_repo.commit("api", {"src/pkg/core.py": pragma_core.replace("(fp)", "(fp, strict=False)")})
    git_repo.commit("rust fix with a test", {"src/lib.rs": LIB_V2})
    rust, api, pragma, _ = walk(git_repo.root)
    assert unhashed(pragma.files[0].patch) == PatchStats(1, 0, 0, 0)
    assert unhashed(api.files[0].patch) == PatchStats(1, 1, 1, 1, api=("def load",))
    assert unhashed(rust.files[0].patch) == PatchStats(2, 1, 1, 1, test_added=5, asserts=1)
    # Without file contents the test module cannot be found: every line is code.
    (rust_no_content, *_) = walk(git_repo.root, content=False)
    assert unhashed(rust_no_content.files[0].patch) == PatchStats(2, 2, 5, 1, asserts=1)


def test_walk_ignores_repository_diff_settings(git_repo: GitRepo) -> None:
    git_repo.commit("base", {"a.go": lines(12)})
    changed = lines(12).replace("line 2\n", "line two\n").replace("line 5\n", "line five\n")
    git_repo.commit("two edits three lines apart", {"a.go": changed})
    for key, value in [
        ("diff.interHunkContext", "10"),
        ("diff.algorithm", "patience"),
        ("diff.noprefix", "true"),
        ("diff.context", "8"),
    ]:
        git_repo.git("config", key, value)
    edit, _ = walk(git_repo.root)
    assert unhashed(edit.files[0].patch) == PatchStats(2, 2, 2, 2)


def test_walk_measures_deleted_rust_test_modules(git_repo: GitRepo) -> None:
    git_repo.commit("base", {"src/lib.rs": LIB_V1})
    without_tests = LIB_V1.split("#[cfg(test)]")[0]
    git_repo.commit("drop the tests", {"src/lib.rs": without_tests})
    drop, _ = walk(git_repo.root)
    assert unhashed(drop.files[0].patch) == PatchStats(1, 0, 0, 0, test_deleted=7)


AREA = """\
/// The scaled area of a rectangle.
pub fn area(width_in_pixels: u32, height_in_pixels: u32, scale_factor: u32) -> u32 {
    width_in_pixels
        * height_in_pixels
}
"""
AREA_TEST = "use area::area;\n\n#[test]\nfn unscaled() {\n    assert_eq!(area(2, 3, 1), 6);\n}\n"
SCALED_TEST = "\n#[test]\nfn scaled() {\n    assert_eq!(area(2, 3, 2), 12);\n}\n"


def test_an_operator_first_line_is_a_real_fix(git_repo: GitRepo) -> None:
    git_repo.commit("area", {"src/lib.rs": AREA, "tests/area.rs": AREA_TEST})
    fixed = AREA.replace("height_in_pixels\n}", "height_in_pixels\n        * scale_factor\n}")
    git_repo.commit(
        "Apply the scale factor", {"src/lib.rs": fixed, "tests/area.rs": AREA_TEST + SCALED_TEST}
    )
    for content in (True, False):
        commit, _ = walk(git_repo.root, content=content)
        outcome = evaluate(commit, Settings())
        assert isinstance(outcome, Candidate), outcome
        lib = next(f for f in commit.files if f.path == "src/lib.rs")
        assert lib.patch is not None
        assert (lib.patch.code_hunks, lib.patch.code_added) == (1, 1)


JAVADOC = """\
package demo;

/**
 * Parses rates.
 *
 * @return the rate
 */
public final class Rates {
  public static double rate(double base) {
    return base
        * 2;
  }
}
"""


def test_javadoc_lines_are_comments_when_the_contents_say_so(git_repo: GitRepo) -> None:
    git_repo.commit("rates", {"src/main/java/demo/Rates.java": JAVADOC})
    git_repo.commit(
        "Reword the docs",
        {
            "src/main/java/demo/Rates.java": JAVADOC.replace("the rate", "the doubled rate"),
            "src/test/java/demo/RatesTest.java": "class RatesTest {}\n",
        },
    )
    docs, _ = walk(git_repo.root)
    outcome = evaluate(docs, Settings())
    assert isinstance(outcome, Rejection)
    assert outcome.reason is RejectReason.SOURCE_COSMETIC
    # Without the contents the line could be code: it is kept as code.
    docs_no_content, _ = walk(git_repo.root, content=False)
    assert isinstance(evaluate(docs_no_content, Settings()), Candidate)


def test_contents_cut_at_the_read_limit_do_not_place_star_lines(
    git_repo: GitRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_repo.commit("rates", {"src/main/java/demo/Rates.java": JAVADOC})
    git_repo.commit("Reword", {"src/main/java/demo/Rates.java": JAVADOC.replace("Parses", "Reads")})
    monkeypatch.setattr(gitlog, "MAX_CONTENT", len(JAVADOC))
    reword, _ = walk(git_repo.root)
    (rates,) = reword.files
    assert rates.patch is not None
    assert rates.patch.code_hunks == 1
