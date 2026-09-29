"""Patch fingerprints: whitespace, path and line-number insensitive, on real synthetic repos."""

from __future__ import annotations

import subprocess

import pytest

from commitminer.fingerprint import (
    FINGERPRINT_VERSION,
    HASH_CHARS,
    Fingerprint,
    fingerprint,
    hunk_hash,
    normalize_line,
    overlap,
    patch_hash,
)
from commitminer.gitlog import walk
from commitminer.models import Commit, FileChange, PatchStats
from commitminer.scoring import Candidate, evaluate
from commitminer.settings import Settings
from commitminer.stats import diff_stats
from gitrepo import GitRepo

PARSE = '''"""Parse short durations such as 1h30m into seconds."""

UNITS = {"s": 1, "m": 60, "h": 3600}


def parse(text):
    total = 0
    number = ""
    for char in text:
        if char.isdigit():
            number += char
        elif char in UNITS:
            total += int(number) * UNITS[char]
            number = ""
        else:
            raise ValueError(f"bad unit {char!r}")
    return total
'''
PARSE_FIXED = PARSE.replace(
    "        elif char in UNITS:\n",
    "        elif char in UNITS:\n"
    "            if not number:\n"
    '                raise ValueError(f"missing number before {char!r}")\n',
).replace(
    "    return total\n",
    '    if number:\n        raise ValueError(f"missing unit after {number}")\n    return total\n',
)
TESTS = (
    "import pytest\n\nfrom durations.parse import parse\n\n\n"
    "def test_parse():\n    assert parse('1h30m') == 5400\n"
)
TESTS_FIXED = TESTS + (
    "\n\ndef test_parse_rejects_a_bare_number():\n"
    "    with pytest.raises(ValueError):\n"
    "        parse('90')\n"
)
HEADER = "# SPDX-License-Identifier: MIT\n# Copyright the durations authors.\n\n"
SRC, TEST = "src/durations/parse.py", "tests/test_parse.py"


def two_spaces(text: str) -> str:
    """Re-indent four-space Python as two-space Python (the same program)."""
    out = []
    for line in text.splitlines(keepends=True):
        stripped = line.lstrip(" ")
        out.append(" " * ((len(line) - len(stripped)) // 2) + stripped)
    return "".join(out)


def hunk_headers(repo: GitRepo, sha: str) -> list[str]:
    """The ``@@ -a,b +c,d @@`` headers of a commit's --unified=0 patch."""
    text = repo.git("show", "--format=", "--unified=0", sha)
    return [line.split(" @@")[0] for line in text.splitlines() if line.startswith("@@ ")]


def fix_of(repo: GitRepo) -> Commit:
    """The newest commit of ``repo``, walked with contents."""
    return walk(repo.root, max_count=1)[0]


def candidate(commit: Commit) -> Candidate:
    outcome = evaluate(commit, Settings())
    assert isinstance(outcome, Candidate), outcome
    return outcome


def print_of(commit: Commit) -> Fingerprint:
    value = candidate(commit).fingerprint
    assert value is not None
    return value


@pytest.fixture
def upstream(git_repo: GitRepo) -> GitRepo:
    git_repo.commit("Add duration parser", {SRC: PARSE, TEST: TESTS, "README.md": "durations\n"})
    base = git_repo.head()
    git_repo.commit(
        "Reject numbers without a unit (fixes #7)",
        {SRC: PARSE_FIXED, TEST: TESTS_FIXED, "CHANGELOG.md": "- reject 90\n"},
    )
    git_repo.git("branch", "release", base)
    return git_repo


# --- normalisation and hashing ----------------------------------------------------------


def test_hunk_hashes_ignore_whitespace_but_not_content() -> None:
    assert normalize_line("\t if  x :\t # note ") == "if x : # note"
    first = hunk_hash(["    return a + b"], ["    return a  +  b + c"])
    assert first == hunk_hash(["\treturn a + b "], ["\t\treturn a + b\t+ c", "   "])
    assert first is not None
    assert len(first) == HASH_CHARS
    assert first != hunk_hash(["    return a + b"], ["    return a + b - c"])
    # A space where there was none is a change (a format string, for example).
    assert hunk_hash(['    "{} {}"'], ['    "{}{}"']) is not None
    # Deleted and added lines are kept apart: moving a line from - to + is a change.
    assert hunk_hash(["x"], []) != hunk_hash([], ["x"])


def test_whitespace_only_hunks_have_no_hash() -> None:
    assert hunk_hash(["    return x"], ["\treturn x"]) is None
    assert hunk_hash(["x = {a:  1,"], ["x = {a: 1,  "]) is None
    assert hunk_hash([], ["", "   "]) is None
    assert hunk_hash(["a", "", "b"], ["a", "b"]) is None


def test_patch_hash_ignores_order_but_keeps_repeats() -> None:
    assert patch_hash(["b", "a"]) == patch_hash(["a", "b"])
    assert patch_hash(["a", "a"]) != patch_hash(["a"])
    value = Fingerprint.of(["b", "a", "a"])
    assert value.hunks == ("a", "a", "b")
    assert value.distinct == frozenset({"a", "b"})
    assert FINGERPRINT_VERSION == 1


def test_overlap_is_measured_against_the_smaller_set() -> None:
    assert overlap(["a", "b", "c"], ["a", "b"]) == (2, 1.0)
    assert overlap(["a", "b", "c"], ["a", "x", "y", "z"]) == (1, 1 / 3)
    assert overlap(["a", "a"], ["a"]) == (1, 1.0)
    assert overlap([], ["a"]) == (0, 0.0)


# --- which files count --------------------------------------------------------------------


def _file(path: str, hashes: tuple[str, ...] | None, added: int | None = 1) -> FileChange:
    patch = None if added is None else PatchStats(1, 1, added, 0, hunk_hashes=hashes)
    return FileChange(path, added, None if added is None else 0, patch=patch)


def test_only_source_and_test_hunks_count() -> None:
    stats = diff_stats(
        Commit(
            "a" * 40,
            (),
            "2024-01-01T00:00:00+00:00",
            "Fix",
            (
                _file("src/pkg/a.py", ("s1",)),
                _file("tests/test_a.py", ("t1", "t2")),
                _file("CHANGELOG.md", ("doc",)),
                _file(".github/workflows/ci.yml", ("ci",)),
                _file("tests/data/img.png", None, added=None),
            ),
        )
    )
    assert fingerprint(stats) == Fingerprint.of(["s1", "t1", "t2"])


@pytest.mark.parametrize(
    "files",
    [
        # A recording made before fingerprints: the hashes are unknown, not empty.
        (_file("src/pkg/a.py", None), _file("tests/test_a.py", ("t1",))),
        # Every source and test hunk only changed whitespace.
        (_file("src/pkg/a.py", ()), _file("tests/test_a.py", ())),
    ],
)
def test_no_fingerprint_without_hashes(files: tuple[FileChange, ...]) -> None:
    commit = Commit("a" * 40, (), "2024-01-01T00:00:00+00:00", "Fix", files)
    assert fingerprint(diff_stats(commit)) is None


# --- the same fix in other places ---------------------------------------------------------


def test_a_cherry_pick_onto_a_shifted_file_has_the_same_fingerprint(upstream: GitRepo) -> None:
    fix = fix_of(upstream)
    upstream.git("checkout", "-q", "release")
    upstream.commit("Add license headers", {SRC: HEADER + PARSE})
    upstream.git("cherry-pick", "-x", fix.sha)
    picked = fix_of(upstream)
    assert picked.sha != fix.sha
    assert "(cherry picked from commit" in picked.message
    assert print_of(picked) == print_of(fix)
    assert len(print_of(fix).hunks) == 3
    # The source hunks moved down by the three header lines; only the positions differ.
    assert hunk_headers(upstream, picked.sha) != hunk_headers(upstream, fix.sha)


def test_a_reindented_copy_has_the_same_fingerprint(upstream: GitRepo) -> None:
    fix = fix_of(upstream)
    fork = GitRepo(upstream.root.parent / "fork")
    fork.commit("Import the parser", {SRC: two_spaces(PARSE), TEST: two_spaces(TESTS)})
    fork.commit(
        "Reject numbers without a unit",
        {SRC: two_spaces(PARSE_FIXED), TEST: two_spaces(TESTS_FIXED)},
    )
    copy = fix_of(fork)
    assert "\n  if number:\n    raise" in two_spaces(PARSE_FIXED)
    assert copy.sha != fix.sha
    assert print_of(copy) == print_of(fix)


def test_rename_only_variants_have_the_same_fingerprint(upstream: GitRepo) -> None:
    fix = fix_of(upstream)
    fork = GitRepo(upstream.root.parent / "moved")
    fork.commit("Import the parser", {SRC: PARSE, TEST: TESTS})
    fork.git("mv", "src/durations", "lib")
    fork.git("mv", TEST, "tests/test_durations.py")
    fork.commit("Move the sources to lib/")
    moved = fix_of(fork)
    assert {f.old_path for f in moved.files} == {"src/durations/parse.py", TEST}
    # A pure move changes no hunk: it is not a candidate and has nothing to hash.
    assert all(f.patch is not None and f.patch.hunk_hashes == () for f in moved.files)
    fork.commit(
        "Reject numbers without a unit",
        {"lib/parse.py": PARSE_FIXED, "tests/test_durations.py": TESTS_FIXED},
    )
    assert print_of(fix_of(fork)) == print_of(fix)
    # Moving and fixing in one commit: rename detection keeps only the real hunks.
    both = GitRepo(upstream.root.parent / "both")
    both.commit("Import the parser", {SRC: PARSE, TEST: TESTS})
    both.git("mv", "src/durations", "pkg")
    both.commit("Move and fix", {"pkg/parse.py": PARSE_FIXED, TEST: TESTS_FIXED})
    moved_and_fixed = fix_of(both)
    assert any(f.old_path == SRC for f in moved_and_fixed.files)
    assert print_of(moved_and_fixed) == print_of(fix)


def test_a_cherry_pick_with_a_resolved_conflict_overlaps_partially(upstream: GitRepo) -> None:
    fix = fix_of(upstream)
    upstream.git("checkout", "-q", "release")
    plain = 'raise ValueError("bad unit")'
    diverged = PARSE.replace('raise ValueError(f"bad unit {char!r}")', plain)
    upstream.commit("Use plain error messages", {SRC: diverged})
    with pytest.raises(subprocess.CalledProcessError):
        upstream.git("cherry-pick", "-x", fix.sha)
    # The conflict is resolved in the branch's style: one of the three hunks differs.
    resolved = PARSE_FIXED.replace('raise ValueError(f"bad unit {char!r}")', plain).replace(
        'raise ValueError(f"missing unit after {number}")', 'raise ValueError("missing unit")'
    )
    upstream.write({SRC: resolved})
    upstream.git("add", "-A")
    upstream.git("-c", "core.editor=true", "cherry-pick", "--continue")
    picked = print_of(fix_of(upstream))
    original = print_of(fix)
    assert picked.patch != original.patch
    shared, share = overlap(picked.hunks, original.hunks)
    assert (shared, round(share, 2)) == (2, 0.67)


def test_a_different_fix_shares_no_hunk(upstream: GitRepo) -> None:
    fix = fix_of(upstream)
    upstream.commit(
        "Accept days (fixes #9)",
        {
            SRC: PARSE_FIXED.replace('"h": 3600}', '"h": 3600, "d": 86400}'),
            TEST: TESTS_FIXED + "\n\ndef test_days():\n    assert parse('1d') == 86400\n",
        },
    )
    assert overlap(print_of(fix_of(upstream)).hunks, print_of(fix).hunks) == (0, 0.0)
