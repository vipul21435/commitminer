"""Glob patterns for classifier rules: checked when a rule is built, matched in linear time.

Syntax:

- ``*`` any run of characters within one path component, ``?`` one character;
- ``[...]`` a character class, ``[!...]`` its negation (as in :mod:`fnmatch`);
- ``{a,b}`` alternatives (not nested; the first ``}`` closes the group);
- ``**`` as a whole path segment: any number of directories. ``**/x`` also
  matches ``x`` at the root; a trailing ``/**`` needs at least one more
  component (``.github/**`` matches everything under ``.github/``). Elsewhere
  ``**`` is an ordinary ``*``, as in ``.gitignore``.

Paths use ``/``; there is no escape character, so a backslash can never match.

Matching cannot backtrack exponentially, whatever the pattern:

- within a component, stars are translated by :func:`fnmatch.translate`, which
  places each ``*fixed`` piece with an atomic group at its first fit;
- across components, the blocks of segments between ``**`` segments are placed
  left to right at their first fit. That is exact, because the ``**`` after a
  block can absorb any gap a later fit would have left.

:func:`check_pattern` rejects patterns that could never match or are too large,
so a typo in ``commitminer.toml`` is an error instead of a rule that silently
does nothing.
"""

from __future__ import annotations

import fnmatch
import functools
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

MAX_ALTERNATIVES: Final = 256
"""Most patterns one glob may expand to through ``{a,b}`` groups."""

GLOBSTAR: Final = "**"

PatternKind = Literal["component", "path"]


class GlobError(ValueError):
    """A glob pattern is malformed or can never match."""


def _class_end(pattern: str, start: int) -> int | None:
    """Index of the ``]`` closing the class that opens at ``start``, as fnmatch finds it."""
    j = start + 1
    if j < len(pattern) and pattern[j] == "!":
        j += 1
    if j < len(pattern) and pattern[j] == "]":
        j += 1
    end = pattern.find("]", j)
    return end if end != -1 else None


def expand_braces(pattern: str) -> tuple[str, ...]:
    """Expand ``{a,b}`` groups outside character classes into plain patterns, in order."""
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "[" and (end := _class_end(pattern, i)) is not None:
            i = end + 1
            continue
        if char == "{" and (close := pattern.find("}", i + 1)) != -1:
            head, rest = pattern[:i], expand_braces(pattern[close + 1 :])
            options = pattern[i + 1 : close].split(",")
            expanded = tuple(head + option + tail for option in options for tail in rest)
            if len(expanded) > MAX_ALTERNATIVES:
                raise GlobError(
                    f"{pattern!r}: expands to more than {MAX_ALTERNATIVES} alternatives"
                )
            return expanded
        i += 1
    return (pattern,)


def _check_classes(pattern: str) -> None:
    """Reject character classes with an empty range such as ``[z-a]``."""
    i = 0
    while i < len(pattern):
        if pattern[i] != "[" or (end := _class_end(pattern, i)) is None:
            i += 1
            continue
        body = pattern[i + 1 : end].removeprefix("!")
        k = 0
        while k < len(body):
            if k + 2 < len(body) and body[k + 1] == "-":
                low, high = body[k], body[k + 2]
                if low > high:
                    raise GlobError(f"{pattern!r}: empty character range {low}-{high}")
                k += 3
            else:
                k += 1
        i = end + 1


def check_pattern(pattern: str, kind: PatternKind) -> None:
    """Raise :class:`GlobError` for a pattern that is malformed or can never match.

    A ``component`` pattern (``dir`` and ``name`` rules) is matched against
    one path component, so it cannot contain ``/``. A ``path`` pattern is
    relative to the repository root: no leading or trailing ``/``, no empty
    segment.
    """
    if not pattern:
        raise GlobError("empty pattern")
    if "\\" in pattern:
        raise GlobError(f"{pattern!r}: a backslash can never match (paths use /)")
    if kind == "component" and "/" in pattern:
        raise GlobError(
            f"{pattern!r}: a directory or file name cannot contain '/' (use a path pattern)"
        )
    if kind == "path" and (pattern.startswith("/") or pattern.endswith("/")):
        raise GlobError(
            f"{pattern!r}: paths are relative to the root, without a leading or trailing '/'"
        )
    for alternative in expand_braces(pattern):
        if not alternative:
            raise GlobError(f"{pattern!r}: an alternative is empty")
        if kind == "path" and "" in alternative.split("/"):
            raise GlobError(f"{pattern!r}: empty path segment in {alternative!r}")
        _check_classes(alternative)


@functools.cache
def component_regex(pattern: str, case_sensitive: bool = False) -> re.Pattern[str]:
    """A regex that matches one path component against ``pattern`` (use ``.match``)."""
    alternatives = expand_braces(pattern)
    regex = "|".join(fnmatch.translate(alternative) for alternative in alternatives)
    return re.compile(regex, 0 if case_sensitive else re.IGNORECASE)


_ANY: Final = re.compile(r"(?s:.*)\Z")
"""Matches any single component; stands in for the component a trailing ``/**`` needs."""

Block = tuple[re.Pattern[str], ...]


def _required(alternative: str, case_sensitive: bool) -> frozenset[str]:
    """Plain ASCII components that any matching path must contain (a cheap first check)."""
    return frozenset(
        segment if case_sensitive else segment.lower()
        for segment in alternative.split("/")
        if segment != GLOBSTAR and segment.isascii() and not any(c in segment for c in "*?[")
    )


def _blocks(alternative: str, case_sensitive: bool) -> tuple[Block, ...]:
    """Split one brace-free pattern into blocks of component matchers between ``**``s."""
    segments: list[str] = []
    for segment in alternative.split("/"):
        if segment == GLOBSTAR and segments and segments[-1] == GLOBSTAR:
            continue  # "**/**" is the same as "**"
        segments.append(segment)
    blocks: list[list[re.Pattern[str]]] = [[]]
    for segment in segments:
        if segment == GLOBSTAR:
            blocks.append([])
        else:
            blocks[-1].append(component_regex(segment, case_sensitive))
    if segments[-1] == GLOBSTAR:
        blocks[-2].append(_ANY)  # a trailing "/**" needs at least one component
    return tuple(tuple(block) for block in blocks)


def _fits(block: Block, parts: Sequence[str], start: int) -> bool:
    return all(matcher.match(parts[start + k]) for k, matcher in enumerate(block))


def _match_blocks(blocks: tuple[Block, ...], parts: Sequence[str]) -> bool:
    if len(blocks) == 1:
        return len(blocks[0]) == len(parts) and _fits(blocks[0], parts, 0)
    first, middle, last = blocks[0], blocks[1:-1], blocks[-1]
    low, high = len(first), len(parts) - len(last)
    if low > high or not _fits(first, parts, 0) or not _fits(last, parts, high):
        return False
    position = low
    for block in middle:
        while position + len(block) <= high and not _fits(block, parts, position):
            position += 1
        if position + len(block) > high:
            return False
        position += len(block)
    return True


@dataclass(frozen=True, slots=True)
class PathGlob:
    """A compiled path pattern: per ``{a,b}`` alternative, its blocks and plain components."""

    pattern: str
    case_sensitive: bool
    alternatives: tuple[tuple[tuple[Block, ...], frozenset[str]], ...]

    def match(self, parts: Sequence[str], present: frozenset[str] | None = None) -> bool:
        """True when the path split into ``parts`` (its ``/``-separated components) matches.

        ``present`` may pass the set of components (lower-cased unless the
        glob is case-sensitive) to skip alternatives that cannot match.
        """
        if present is None:
            present = frozenset(parts if self.case_sensitive else (p.lower() for p in parts))
        return any(
            required <= present and _match_blocks(blocks, parts)
            for blocks, required in self.alternatives
        )

    def match_path(self, path: str) -> bool:
        """:meth:`match` for a ``/``-separated path."""
        return self.match(path.split("/"))


@functools.cache
def path_glob(pattern: str, case_sensitive: bool = False) -> PathGlob:
    """Compile a path pattern (checked with :func:`check_pattern` first)."""
    check_pattern(pattern, "path")
    return PathGlob(
        pattern,
        case_sensitive,
        tuple(
            (_blocks(alternative, case_sensitive), _required(alternative, case_sensitive))
            for alternative in expand_braces(pattern)
        ),
    )
