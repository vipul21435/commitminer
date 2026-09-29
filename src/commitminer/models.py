"""Typed records for walked history: one commit and the files it changed."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FileChange:
    """One file changed by a commit, as reported by ``git log --numstat``.

    ``old_path`` is set only for renames (detected with ``-M``). ``added`` and
    ``deleted`` are ``None`` for binary files, which git reports as ``-``.
    """

    path: str
    added: int | None
    deleted: int | None
    old_path: str | None = None

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
