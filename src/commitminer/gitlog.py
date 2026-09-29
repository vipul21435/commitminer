"""History walker over a local clone: one ``git log`` call, parsed exactly.

git is run with a fixed environment and configuration so that user or repository
settings (pagers, colors, external diff drivers, signature display, fsmonitor
hooks) cannot change the output. The output is NUL-separated (``-z``), so paths
with spaces, quotes, newlines or non-UTF-8 bytes are parsed without unquoting.

Layout of the output, one block per commit, with ``\\0`` shown as ``|``::

    |<MARKER>|<sha>|<parents>|<author date>|<message>|
    \\n<added>\\t<deleted>\\t<path>|                       (plain change)
    <added>\\t<deleted>\\t|<old path>|<new path>|         (rename)

Commit messages cannot contain NUL, so every header field is exactly one token,
and numstat entries are self-delimiting (``^(\\d+|-)\\t(\\d+|-)\\t``), so the
parser always knows how many path tokens follow. A path that happens to equal
the marker is consumed positionally and cannot start a new commit.
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from pathlib import Path

from commitminer.models import Commit, FileChange

MARKER = "commitminer:v1"
_FORMAT = f"--format=%x00{MARKER}%x00%H%x00%P%x00%aI%x00%B"
_NUMSTAT = re.compile(r"^(\d+|-)\t(\d+|-)\t(.*)$", re.DOTALL)
_ENCODING = "utf-8"
_ERRORS = "surrogateescape"

GIT_ENV = {
    "LC_ALL": "C",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_PAGER": "cat",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_OPTIONAL_LOCKS": "0",
}
"""Environment overrides for every git call (no system or global config)."""

GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "color.ui=never",
)
"""Command-line config that wins over any repository-local setting."""


class GitError(RuntimeError):
    """git failed or produced output the parser does not understand."""


def log_command(repo: Path, rev: str = "HEAD", max_count: int | None = None) -> list[str]:
    """Build the ``git log`` argument list used by :func:`walk`."""
    cmd = ["git", "-C", str(repo), *GIT_CONFIG, "log", "--no-merges", "-M", "-z", "--numstat"]
    cmd += ["--no-ext-diff", "--no-textconv", "--no-color", _FORMAT]
    if max_count is not None:
        if max_count < 1:
            raise ValueError("max_count must be at least 1")
        cmd.append(f"--max-count={max_count}")
    # --end-of-options stops a revision such as "--output=x" from being read as an option.
    cmd += ["--end-of-options", rev]
    return cmd


def run_git(args: Sequence[str], timeout: float = 600.0) -> bytes:
    """Run git with :data:`GIT_ENV` and return stdout; raise :class:`GitError` on failure."""
    env = {**os.environ, **GIT_ENV}
    try:
        proc = subprocess.run(
            list(args), capture_output=True, env=env, timeout=timeout, check=False
        )
    except FileNotFoundError as exc:
        raise GitError("git executable not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timed out after {timeout:g} s") from exc
    if proc.returncode != 0:
        stderr = proc.stderr.decode(_ENCODING, "replace").strip()
        raise GitError(f"git exited with {proc.returncode}: {stderr}")
    return proc.stdout


def walk(repo: Path, rev: str = "HEAD", max_count: int | None = None) -> list[Commit]:
    """Walk the non-merge history of ``rev`` in the clone at ``repo``, newest first."""
    return list(parse_log(run_git(log_command(repo, rev, max_count))))


def head_sha(repo: Path, rev: str = "HEAD") -> str:
    """Resolve ``rev`` to a full commit sha."""
    out = run_git(
        ["git", "-C", str(repo), *GIT_CONFIG, "rev-parse", "--verify", "--end-of-options", rev]
    )
    return out.decode(_ENCODING).strip()


def normalize_date(date: str) -> str:
    """Spell UTC as ``+00:00``: some git versions print ``Z`` for ``%aI``, others do not."""
    return date[:-1] + "+00:00" if date.endswith("Z") else date


def _decode(token: bytes) -> str:
    return token.decode(_ENCODING, _ERRORS)


def _count(field: str) -> int | None:
    return None if field == "-" else int(field)


def parse_log(data: bytes) -> Iterator[Commit]:
    """Parse the output of :func:`log_command` into commits, in output order."""
    tokens = data.split(b"\0")
    marker = MARKER.encode()
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token == b"":
            i += 1
            continue
        if token != marker:
            raise GitError(f"unexpected token at position {i}: {token[:60]!r}")
        if i + 4 >= len(tokens):
            raise GitError("truncated commit header")
        sha, parents, date, message = (_decode(t) for t in tokens[i + 1 : i + 5])
        i += 5
        files: list[FileChange] = []
        while i < len(tokens) and tokens[i] != b"":
            entry = _decode(tokens[i])
            if not files:
                # git separates the message from the first numstat entry with one newline.
                entry = entry.removeprefix("\n")
            match = _NUMSTAT.match(entry)
            if match is None:
                raise GitError(f"bad numstat entry in {sha}: {entry[:60]!r}")
            added, deleted, path = match.group(1), match.group(2), match.group(3)
            if path:
                files.append(FileChange(path, _count(added), _count(deleted)))
                i += 1
                continue
            if i + 2 >= len(tokens) or not tokens[i + 1] or not tokens[i + 2]:
                raise GitError(f"truncated rename entry in {sha}")
            old_path, new_path = _decode(tokens[i + 1]), _decode(tokens[i + 2])
            files.append(FileChange(new_path, _count(added), _count(deleted), old_path))
            i += 3
        yield Commit(
            sha=sha,
            parents=tuple(parents.split()),
            date=normalize_date(date),
            message=message,
            files=tuple(files),
        )
