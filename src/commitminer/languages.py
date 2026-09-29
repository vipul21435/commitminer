"""The programming languages CommitMiner classifies, detected by file extension."""

from __future__ import annotations

from enum import StrEnum
from typing import Final


class Language(StrEnum):
    """A language the classifier has rules for."""

    PYTHON = "python"
    RUST = "rust"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    GO = "go"
    JAVA = "java"


EXTENSIONS: Final[dict[str, Language]] = {
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".pyx": Language.PYTHON,
    ".pxd": Language.PYTHON,
    ".rs": Language.RUST,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".mjs": Language.JAVASCRIPT,
    ".cjs": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".mts": Language.TYPESCRIPT,
    ".cts": Language.TYPESCRIPT,
    ".go": Language.GO,
    ".java": Language.JAVA,
}
"""Lower-case file extension to language."""


def language_of(path: str) -> Language | None:
    """The language of a path by its extension (case-insensitive), or ``None``."""
    name = path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = name.rfind(".")
    if dot <= 0:
        # No extension, or a dot file such as ".js" with nothing before the dot.
        return None
    return EXTENSIONS.get(name[dot:].lower())
