"""Typed records for walked history: one commit and the files it changed."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PatchStats:
    """What the ``--unified=0`` patch of one file says, beyond its line counts.

    ``hunks`` counts every hunk. The ``code_*`` fields leave out blank lines,
    comment-only lines, hunks that only change comments or whitespace (see
    :mod:`commitminer.patch`) and lines inside inline test modules; for a file
    in no known language they equal the raw counts. ``test_added`` and
    ``test_deleted`` are lines inside Rust ``#[cfg(test)]`` modules. ``asserts``
    counts added assertion lines (only inside the inline test modules when the
    file has any). ``api`` names public declarations that the code hunks add,
    remove or change, such as ``def load`` or ``pub fn parse``.
    """

    hunks: int
    code_hunks: int
    code_added: int
    code_deleted: int
    test_added: int = 0
    test_deleted: int = 0
    asserts: int = 0
    api: tuple[str, ...] = ()

    @property
    def code_lines(self) -> int:
        """Code lines added plus deleted."""
        return self.code_added + self.code_deleted

    @property
    def test_lines(self) -> int:
        """Inline test lines added plus deleted."""
        return self.test_added + self.test_deleted


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file changed by a commit, as reported by ``git log --numstat``.

    ``old_path`` is set only for renames (detected with ``-M``). ``added`` and
    ``deleted`` are ``None`` for binary files, which git reports as ``-``.
    ``signals`` are the sorted content signals of the file at this commit (see
    :mod:`commitminer.signals`); empty when content was not read. ``patch`` is
    ``None`` when the patch was not read (hand-built records, old recordings)
    and for binary files.
    """

    path: str
    added: int | None
    deleted: int | None
    old_path: str | None = None
    signals: tuple[str, ...] = ()
    patch: PatchStats | None = None

    @property
    def binary(self) -> bool:
        """True when git reported the file as binary (no line counts)."""
        return self.added is None or self.deleted is None

    @property
    def changed_lines(self) -> int:
        """Added plus deleted lines; zero for binary files."""
        return (self.added or 0) + (self.deleted or 0)


@dataclass(frozen=True, slots=True)
class Commit:
    """One non-merge commit with its changed files.

    ``date`` is the author date in strict ISO 8601 (``%aI``), including the
    author's UTC offset. ``message`` is the raw commit message (``%B``).
    """

    sha: str
    parents: tuple[str, ...]
    date: str
    message: str
    files: tuple[FileChange, ...]

    def _paragraphs(self) -> tuple[list[str], list[str]]:
        lines = self.message.strip("\n").splitlines()
        for index, line in enumerate(lines):
            if not line.strip():
                return lines[:index], lines[index + 1 :]
        return lines, []

    @property
    def subject(self) -> str:
        """The first paragraph of the message joined into one line, as git's ``%s``."""
        first, _ = self._paragraphs()
        return " ".join(line.strip() for line in first)

    @property
    def body(self) -> str:
        """Everything after the first paragraph, stripped."""
        _, rest = self._paragraphs()
        return "\n".join(rest).strip()

    @property
    def base(self) -> str | None:
        """The first parent, the commit a fail-to-pass task would start from."""
        return self.parents[0] if self.parents else None
