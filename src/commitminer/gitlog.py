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

Content signals (generated headers, minified JavaScript, Rust inline tests)
need file contents, which ``git log`` does not print. :func:`attach_signals`
reads each changed code file at its commit through one long-running
``git cat-file --batch`` process, one request at a time.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
from collections.abc import Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import IO

from commitminer.languages import language_of
from commitminer.models import Commit, FileChange
from commitminer.signals import (
    MAX_CONTENT,
    RUST_INLINE_TESTS,
    RUST_TESTS_ADDED,
    detect,
    rust_tests_added,
)

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


def walk(
    repo: Path, rev: str = "HEAD", max_count: int | None = None, content: bool = True
) -> list[Commit]:
    """Walk the non-merge history of ``rev`` in the clone at ``repo``, newest first.

    With ``content`` (the default) every changed code file also gets its content
    signals; without it, classification uses path rules only.
    """
    commits = list(parse_log(run_git(log_command(repo, rev, max_count))))
    return attach_signals(repo, commits) if content else commits


_BATCH_HEADER = re.compile(rb"^[0-9a-f]{40,64} ([a-z]+) (\d+)\n$")
_CHUNK = 1 << 16


class BlobReader:
    """Read file versions through one ``git cat-file --batch`` process.

    Requests are written one at a time and each answer is read in full before
    the next request, so the pipes never fill up. Only the first ``limit``
    bytes of each object are kept; the rest is read and dropped.
    """

    def __init__(self, repo: Path, limit: int = MAX_CONTENT) -> None:
        env = {**os.environ, **GIT_ENV}
        cmd = ["git", "-C", str(repo), *GIT_CONFIG, "cat-file", "--batch"]
        try:
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError as exc:
            raise GitError("git executable not found on PATH") from exc
        self._limit = limit

    def __enter__(self) -> BlobReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    @staticmethod
    def _stream(stream: IO[bytes] | None) -> IO[bytes]:
        assert stream is not None  # Popen was created with PIPE for all three
        return stream

    def _stopped(self) -> GitError:
        self._proc.kill()
        self._proc.wait()
        stderr = self._stream(self._proc.stderr).read().decode(_ENCODING, "replace").strip()
        return GitError(f"git cat-file stopped: {stderr or 'no error message'}")

    def read(self, rev: str, path: str) -> bytes | None:
        """The first bytes of ``path`` at ``rev``, or ``None`` if no such file exists there."""
        if "\n" in path:
            # The batch protocol is line based; such paths cannot be requested.
            return None
        stdin, stdout = self._stream(self._proc.stdin), self._stream(self._proc.stdout)
        request = f"{rev}:{path}".encode(_ENCODING, _ERRORS)
        try:
            stdin.write(request + b"\n")
            stdin.flush()
        except BrokenPipeError:
            raise self._stopped() from None
        header = stdout.readline()
        if not header:
            raise self._stopped()
        match = _BATCH_HEADER.match(header)
        if match is None:
            if header.endswith((b" missing\n", b" ambiguous\n")):
                return None
            raise GitError(f"unexpected git cat-file output: {header[:80]!r}")
        size = int(match.group(2))
        keep = stdout.read(min(size, self._limit))
        remaining = size - len(keep) + 1  # the object is followed by one newline
        while remaining > 0:
            chunk = stdout.read(min(remaining, _CHUNK))
            if not chunk:
                raise self._stopped()
            remaining -= len(chunk)
        return keep if match.group(1) == b"blob" else None

    def close(self) -> None:
        """Close stdin so git exits, then reap it."""
        # A request still buffered for a git that already died cannot be flushed;
        # that failure is not worth masking the error that got us here.
        with contextlib.suppress(BrokenPipeError):
            self._stream(self._proc.stdin).close()
        if self._proc.poll() is None:
            try:
                self._proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
        self._stream(self._proc.stdout).close()
        self._stream(self._proc.stderr).close()


def file_signals(commit: Commit, change: FileChange, blobs: BlobReader) -> FileChange:
    """``change`` with the content signals of the file at ``commit`` filled in.

    A file deleted by the commit is judged as it was in the parent. A Rust file
    with inline tests is also compared with its parent version: if it gained
    ``#[test]`` functions, it gets ``rust-tests-added``.
    """
    language = language_of(change.path)
    if language is None or change.binary:
        return change
    parent = commit.base
    before = change.old_path or change.path
    content = blobs.read(commit.sha, change.path)
    if content is None:
        old = blobs.read(parent, before) if parent else None
        signals = detect(language, old) if old is not None else ()
    else:
        signals = detect(language, content)
        if RUST_INLINE_TESTS in signals:
            previous = blobs.read(parent, before) if parent else None
            if rust_tests_added(content, previous or b""):
                signals = tuple(sorted((*signals, RUST_TESTS_ADDED)))
    return replace(change, signals=signals) if signals else change


def attach_signals(repo: Path, commits: Sequence[Commit]) -> list[Commit]:
    """Fill in content signals for every changed code file of ``commits``."""
    with BlobReader(repo) as blobs:
        return [
            replace(commit, files=tuple(file_signals(commit, f, blobs) for f in commit.files))
            for commit in commits
        ]


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
