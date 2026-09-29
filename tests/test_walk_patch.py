"""The walker measures every file's --unified=0 patch (real synthetic repositories)."""

from __future__ import annotations

from commitminer.gitlog import walk
from commitminer.models import PatchStats
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
