"""Patch fingerprints: the same fix, recognised across repositories, forks and cherry-picks.

A **hunk hash** ignores what differs between two applications of the same change:

- whitespace: leading and trailing whitespace is dropped, runs of whitespace
  inside a line count as one space, and blank lines are left out (a re-indented
  copy, tabs instead of spaces, realigned columns, trailing blanks). A space
  that appears where there was none is kept: ``"{} {}"`` to ``"{}{}"`` changes
  a string, so it is a real change;
- line numbers: the hunk header is not hashed (a cherry-pick onto a file that
  has moved on);
- paths: the file name is not hashed (a renamed or moved file, a fork with
  another layout).

A hunk whose lines are equal after collapsing whitespace runs may still be a
real change: ``"{a}  {b}"`` to ``"{a} {b}"`` changes a string literal. Such a
hunk is hashed a second way, with inner whitespace kept verbatim and only
leading and trailing whitespace dropped, so the fix keeps a fingerprint and a
re-indented copy of it still matches. Only a hunk that changes nothing but
indentation, trailing whitespace or blank lines gets no hash. Hunk hashes are
computed while walking, from the ``--unified=0`` patch, and stored with the
other patch measurements.

A commit's :class:`Fingerprint` is built from the hunks of its source and test
files, the fix and its tests; changelog, docs and CI edits are left out, since
a fork or a backport often has its own. Its ``patch`` hash is the hash of the
sorted hunk hashes, so file and hunk order do not matter either: two commits
with the same patch hash are the same fix. Commits that share most but not all
hunks overlap partially (a cherry-pick with a resolved conflict, a squash of
the fix with more work); :func:`overlap` measures that.
"""

from __future__ import annotations

import hashlib
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from typing import Final

from commitminer.classify import Category
from commitminer.stats import DiffStats

FINGERPRINT_VERSION: Final = 2
"""Bumped whenever normalisation or hashing changes: fingerprints of two versions differ.

2: a hunk that only changes whitespace inside lines is hashed with its inner
whitespace kept (version 1 gave it no hash at all).
"""

HASH_CHARS: Final = 16
"""Hex digits kept from SHA-256 (64 bits) for hunk and patch hashes."""


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:HASH_CHARS]


def normalize_line(line: str) -> str:
    """``line`` without leading and trailing whitespace, inner runs as one space."""
    return " ".join(line.split())


def _lines_hash(old: list[str], new: list[str], prefix: str = "") -> str:
    return _digest(
        prefix + "".join(f"-{line}\n" for line in old) + "".join(f"+{line}\n" for line in new)
    )


def hunk_hash(deleted: Iterable[str], added: Iterable[str]) -> str | None:
    """The hash of one hunk's normalised lines.

    Lines are compared with whitespace runs collapsed first. When that leaves
    the deleted and added lines equal, the hunk is hashed with inner
    whitespace kept (``INNER`` prefix, so it can never equal a collapsed hash),
    because a run of spaces inside a string literal is content. ``None`` when
    the hunk changed only indentation, trailing whitespace or blank lines.
    """
    deleted, added = list(deleted), list(added)
    old = [text for line in deleted if (text := normalize_line(line))]
    new = [text for line in added if (text := normalize_line(line))]
    if old != new:
        return _lines_hash(old, new)
    old = [text for line in deleted if (text := line.strip())]
    new = [text for line in added if (text := line.strip())]
    if old == new:
        return None
    return _lines_hash(old, new, "INNER\n")


def patch_hash(hunks: Iterable[str]) -> str:
    """The hash of a multiset of hunk hashes (order does not matter)."""
    return _digest("".join(f"{hunk}\n" for hunk in sorted(hunks)))


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A commit's fix as hashes: the patch hash and its sorted hunk hashes."""

    patch: str
    hunks: tuple[str, ...]

    @classmethod
    def of(cls, hunks: Iterable[str]) -> Fingerprint:
        """The fingerprint of these hunk hashes (sorted, duplicates kept)."""
        ordered = tuple(sorted(hunks))
        return cls(patch_hash(ordered), ordered)

    @property
    def distinct(self) -> frozenset[str]:
        """The distinct hunk hashes, the unit of partial overlap."""
        return frozenset(self.hunks)


def fingerprint(stats: DiffStats) -> Fingerprint | None:
    """The fingerprint of a commit's source and test hunks.

    ``None`` when a changed source or test text file has no hunk hashes
    (recordings made before fingerprints, hand-built records) or when every
    such hunk only changed indentation, trailing whitespace or blank lines.
    """
    hashes: list[str] = []
    for item in stats.of(Category.SOURCE, Category.TEST):
        patch = item.change.patch
        if item.change.binary:
            continue
        if patch is None or patch.hunk_hashes is None:
            return None
        hashes += patch.hunk_hashes
    return Fingerprint.of(hashes) if hashes else None


def overlap(first: Collection[str], second: Collection[str]) -> tuple[int, float]:
    """Shared distinct hunks, and their share of the smaller of the two sets."""
    a, b = set(first), set(second)
    if not a or not b:
        return 0, 0.0
    shared = len(a & b)
    return shared, shared / min(len(a), len(b))
