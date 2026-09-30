"""History walker over a local clone: one streamed ``git log`` call, parsed exactly.

git is run with a fixed environment and configuration so that user or repository
settings (pagers, colors, external diff drivers, diff algorithms, signature
display, fsmonitor hooks) cannot change the output. The output is NUL-separated
(``-z``), so paths with spaces, quotes, newlines or non-UTF-8 bytes are parsed
without unquoting.

Layout of the output, one block per commit, with ``\\0`` shown as ``|``::

    |<MARKER>|<sha>|<parents>|<author date>|<message>|
    \\n<added>\\t<deleted>\\t<path>|                       (plain change)
    <added>\\t<deleted>\\t|<old path>|<new path>|         (rename)
    |diff --git ...<patch text>                       (all files, --unified=0)

Commit messages cannot contain NUL, so every header field is exactly one token,
and numstat entries are self-delimiting (``^(\\d+|-)\\t(\\d+|-)\\t``), so the
parser always knows how many path tokens follow. A path that happens to equal
the marker is consumed positionally and cannot start a new commit. Patch text
usually has no NUL, but it can: git only checks a file's first 8000 bytes for
NUL before calling it binary, a ``diff`` attribute in ``.gitattributes`` forces
text, and a hunk header copies a line of the file as function context. Patch
text always ends with a newline and a NUL inside it never follows one, so
the parser rejoins the tokens of a patch until one ends with a newline (see
:func:`_patch_text`). The file blocks come in numstat order (a type change,
printed as two blocks, is merged back into one), and each block's line counts
are checked against its numstat entry.

The output is read as a stream, one commit at a time, so memory stays flat on
long histories.

Content signals (generated headers, minified JavaScript, Rust inline tests)
need file contents, which ``git log`` does not print. :func:`file_details`
reads each changed code file at its commit through one long-running
``git cat-file --batch`` process, one request at a time, and measures the
file's patch with :func:`commitminer.patch.analyze`.
"""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
import threading
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import IO

from commitminer.languages import Language, language_of
from commitminer.models import Commit, FileChange, TestFunction
from commitminer.patch import (
    DIFF_HEADER,
    FilePatch,
    PatchError,
    Region,
    analyze,
    comment_regions,
    parse_patch,
    rust_test_regions,
    star_sides,
)
from commitminer.signals import (
    MAX_CONTENT,
    RUST_INLINE_TESTS,
    RUST_TESTS_ADDED,
    detect,
    rust_tests_added,
)
from commitminer.testids import test_functions
from commitminer.urls import strip_credentials

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

GIT_ENV_DROPPED = frozenset(
    {
        # Output shape: GIT_DIFF_OPTS overrides --unified, the others inject config.
        "GIT_DIFF_OPTS",
        "GIT_EXTERNAL_DIFF",
        "GIT_CONFIG",
        "GIT_CONFIG_PARAMETERS",
        "GIT_CONFIG_COUNT",
        # Repository selection: these would win over "git -C <repo>" (set inside git hooks).
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_COMMON_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_NAMESPACE",
    }
)
"""Variables of the caller's environment that never reach git."""

_DROPPED_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def git_env() -> dict[str, str]:
    """The environment for every git call: the caller's, cleaned, plus :data:`GIT_ENV`."""
    kept = {
        key: value
        for key, value in os.environ.items()
        if key not in GIT_ENV_DROPPED and not key.startswith(_DROPPED_PREFIXES)
    }
    return {**kept, **GIT_ENV}


GIT_CONFIG = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "log.showSignature=false",
    "-c",
    "log.showRoot=true",
    "-c",
    "color.ui=never",
)
"""Command-line config that wins over any repository-local setting."""


class GitError(RuntimeError):
    """git failed or produced output the parser does not understand."""


PATCH_OPTIONS = (
    "-p",
    "--unified=0",
    "--inter-hunk-context=0",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--submodule=short",
    "--ignore-submodules=none",
    f"-O{os.devnull}",
)
"""Patch shape: no context lines, and settings a repository config could otherwise change.

``diff.submodule`` would replace a submodule's ``diff --git`` block with a log,
``diff.ignoreSubmodules`` (or ``ignore`` in ``.gitmodules``) would hide
submodule changes, and ``diff.orderFile`` would reorder the files.
"""


def log_command(repo: Path, rev: str = "HEAD", max_count: int | None = None) -> list[str]:
    """Build the ``git log`` argument list used by :func:`walk`."""
    cmd = ["git", "-C", str(repo), *GIT_CONFIG, "log", "--no-merges", "-M", "-z", "--numstat"]
    cmd += [*PATCH_OPTIONS, "--no-ext-diff", "--no-textconv", "--no-color", _FORMAT]
    if max_count is not None:
        if max_count < 1:
            raise ValueError("max_count must be at least 1")
        cmd.append(f"--max-count={max_count}")
    # --end-of-options stops a revision such as "--output=x" from being read as an option.
    cmd += ["--end-of-options", rev]
    return cmd


def run_git(args: Sequence[str], timeout: float = 600.0) -> bytes:
    """Run git with :data:`GIT_ENV` and return stdout; raise :class:`GitError` on failure."""
    env = git_env()
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


_CHUNK = 1 << 16


def _pipe(stream: IO[bytes] | None) -> IO[bytes]:
    assert stream is not None  # Popen was created with PIPE
    return stream


def split_stream(stream: IO[bytes], chunk: int = _CHUNK) -> Iterator[bytes]:
    """Yield the NUL-separated tokens of ``stream``, as ``bytes.split(b"\\0")`` would."""
    parts: list[bytes] = []
    while data := stream.read(chunk):
        pieces = data.split(b"\0")
        parts.append(pieces[0])
        if len(pieces) == 1:
            continue
        yield b"".join(parts)
        yield from pieces[1:-1]
        parts = [pieces[-1]]
    yield b"".join(parts)


@contextlib.contextmanager
def stream_git(args: Sequence[str], timeout: float = 600.0) -> Iterator[Iterator[bytes]]:
    """Run git and yield its stdout as NUL-separated tokens, read as they arrive.

    The caller reads every token; git is killed only if the caller raises.
    stderr goes to a temporary file, so a chatty git cannot block on a full
    pipe. git is killed after ``timeout`` seconds; a non-zero exit or a timeout
    raises :class:`GitError` once the caller is done reading.
    """
    env = git_env()
    with tempfile.TemporaryFile() as errors:
        try:
            proc = subprocess.Popen(list(args), stdout=subprocess.PIPE, stderr=errors, env=env)
        except FileNotFoundError as exc:
            raise GitError("git executable not found on PATH") from exc
        expired = threading.Event()

        def expire() -> None:
            expired.set()
            proc.kill()

        timer = threading.Timer(timeout, expire)
        timer.start()
        stdout = _pipe(proc.stdout)
        try:
            yield split_stream(stdout)
        except BaseException as exc:
            proc.kill()  # the caller gave up; git may be blocked on a full pipe
            if expired.is_set():
                raise GitError(f"git timed out after {timeout:g} s") from exc
            raise
        finally:
            timer.cancel()
            stdout.close()
            proc.wait()
        if expired.is_set():
            raise GitError(f"git timed out after {timeout:g} s")
        if proc.returncode != 0:
            errors.seek(0)
            stderr = errors.read().decode(_ENCODING, "replace").strip()
            raise GitError(f"git exited with {proc.returncode}: {stderr}")


def walk(
    repo: Path, rev: str = "HEAD", max_count: int | None = None, content: bool = True
) -> list[Commit]:
    """Walk the non-merge history of ``rev`` in the clone at ``repo``, newest first.

    Every file gets its patch measured. With ``content`` (the default) every
    changed code file also gets its content signals, and Rust files their
    inline test modules; without it, classification uses path rules only.
    """
    with stream_git(log_command(repo, rev, max_count)) as tokens:
        blobs = BlobReader(repo) if content else None
        try:
            return [complete(c, p, blobs) for c, p in parse_log_tokens(tokens)]
        finally:
            if blobs is not None:
                blobs.close()


_BATCH_HEADER = re.compile(rb"^[0-9a-f]{40,64} ([a-z]+) (\d+)\n$")


class BlobReader:
    """Read file versions through one ``git cat-file --batch`` process.

    Requests are written one at a time and each answer is read in full before
    the next request, so the pipes never fill up. Only the first ``limit``
    bytes of each object are kept; the rest is read and dropped.
    """

    def __init__(self, repo: Path, limit: int = MAX_CONTENT) -> None:
        env = git_env()
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

    _stream = staticmethod(_pipe)

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


def _signals(
    commit: Commit, change: FileChange, language: Language, blobs: BlobReader
) -> tuple[tuple[str, ...], bytes | None, bytes | None]:
    """Signals of a changed code file, and the new and parent contents that were read."""
    parent = commit.base
    before = change.old_path or change.path
    content = blobs.read(commit.sha, change.path)
    if content is None:
        old = blobs.read(parent, before) if parent else None
        return (detect(language, old) if old is not None else ()), None, old
    signals = detect(language, content)
    old = None
    if RUST_INLINE_TESTS in signals:
        old = blobs.read(parent, before) if parent else None
        if rust_tests_added(content, old or b""):
            signals = tuple(sorted((*signals, RUST_TESTS_ADDED)))
    return signals, content, old


def _regions(content: bytes | None) -> tuple[Region, ...]:
    return rust_test_regions(content) if content else ()


def _comments(content: bytes | None, language: Language) -> tuple[Region, ...] | None:
    """Block comment lines of a file version; ``None`` if unread or cut at the read limit."""
    if content is None or len(content) >= MAX_CONTENT:
        return None
    return comment_regions(content, language)


def _tests(content: bytes | None, language: Language) -> tuple[TestFunction, ...] | None:
    """Test functions of a file version; ``None`` if unread or cut at the read limit."""
    if content is None or len(content) >= MAX_CONTENT:
        return None
    return test_functions(content, language)


def file_details(
    commit: Commit,
    change: FileChange,
    patch: FilePatch | None = None,
    blobs: BlobReader | None = None,
) -> FileChange:
    """``change`` with its content signals and patch measurements filled in.

    A file deleted by the commit is judged as it was in the parent. A Rust file
    with inline tests is also compared with its parent version: if it gained
    ``#[test]`` functions, it gets ``rust-tests-added``, and lines inside its
    ``#[cfg(test)]`` modules are measured as test lines. When a changed line
    starts with ``*``, the version it belongs to is lexed to tell a block
    comment continuation from an operator-first line of code. The new version
    also gives the test functions the patch touches.
    """
    language = language_of(change.path)
    signals: tuple[str, ...] = ()
    new = old = None
    if blobs is not None and language is not None and not change.binary:
        signals, new, old = _signals(commit, change, language, blobs)
    if patch is None or change.binary:
        return replace(change, signals=signals, patch=None)
    new_regions: tuple[Region, ...] = ()
    old_regions: tuple[Region, ...] = ()
    new_comments = old_comments = tests = None
    if blobs is not None and language is not None:
        old_stars, new_stars = star_sides(patch, language)
        rust = language is Language.RUST
        if old is None and ((rust and patch.deleted) or old_stars) and commit.base:
            old = blobs.read(commit.base, change.old_path or change.path)
        if rust:
            new_regions, old_regions = _regions(new), _regions(old)
        if new_stars:
            new_comments = _comments(new, language)
        if old_stars:
            old_comments = _comments(old, language)
        tests = _tests(new, language)
    stats = analyze(patch, language, new_regions, old_regions, new_comments, old_comments, tests)
    return replace(change, signals=signals, patch=stats)


def complete(
    commit: Commit, patches: Sequence[FilePatch] | None, blobs: BlobReader | None = None
) -> Commit:
    """Attach signals and patch measurements to every file of a parsed commit.

    ``patches`` must line up with ``commit.files``: one block per numstat entry,
    with the same line counts. A mismatch means the output was misread.
    """
    if patches is not None and len(patches) != len(commit.files):
        raise GitError(
            f"{commit.sha}: {len(commit.files)} numstat entries but {len(patches)} file patches"
        )
    files: list[FileChange] = []
    for index, change in enumerate(commit.files):
        patch = patches[index] if patches is not None else None
        counts = (change.added, change.deleted)
        if patch is not None and not change.binary and (patch.added, patch.deleted) != counts:
            raise GitError(
                f"{commit.sha}: {change.path}: numstat says +{change.added} "
                f"-{change.deleted}, the patch has +{patch.added} -{patch.deleted}"
            )
        files.append(file_details(commit, change, patch, blobs))
    return replace(commit, files=tuple(files))


def head_sha(repo: Path, rev: str = "HEAD") -> str:
    """Resolve ``rev`` to a full commit sha."""
    out = run_git(
        ["git", "-C", str(repo), *GIT_CONFIG, "rev-parse", "--verify", "--end-of-options", rev]
    )
    return out.decode(_ENCODING).strip()


def resolve_commit(repo: Path, rev: str) -> str:
    """Resolve ``rev`` to the full sha of the commit it names (tags are peeled)."""
    return head_sha(repo, f"{rev}^{{commit}}")


_FULL_SHA = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")


def is_ancestor(repo: Path, ancestor: str, commit: str) -> bool | None:
    """Whether ``ancestor`` is ``commit`` or one of its ancestors.

    ``None`` when ``ancestor`` is not a full sha or the clone does not have it
    (a watermark from another clone, or a commit gone after a force push).
    """
    if not _FULL_SHA.match(ancestor):
        return None
    try:
        out = run_git(
            [
                *("git", "-C", str(repo), *GIT_CONFIG, "rev-list", "--count"),
                *("--end-of-options", f"{commit}..{ancestor}"),
            ]
        )
    except GitError:
        return None
    # Nothing reachable from the ancestor that the commit does not also reach.
    return out.strip() == b"0"


def origin_url(repo: Path) -> str | None:
    """The clone's ``remote.origin.url`` without credentials, or ``None`` without an origin.

    A clone made as ``https://<token>@host/...`` keeps the token in its remote;
    it is removed here, since the URL is written to exports and reports.
    """
    try:
        out = run_git(["git", "-C", str(repo), *GIT_CONFIG, "config", "--get", "remote.origin.url"])
    except GitError:
        return None
    return strip_credentials(out.decode(_ENCODING, "replace").strip()) or None


def normalize_date(date: str) -> str:
    """Spell UTC as ``+00:00``: some git versions print ``Z`` for ``%aI``, others do not."""
    return date[:-1] + "+00:00" if date.endswith("Z") else date


def _decode(token: bytes) -> str:
    return token.decode(_ENCODING, _ERRORS)


def _count(field: str) -> int | None:
    return None if field == "-" else int(field)


class _Tokens:
    """A token iterator with lookahead and a position for error messages."""

    def __init__(self, tokens: Iterable[bytes]) -> None:
        self._source = iter(tokens)
        self._ahead: list[bytes] = []
        self.position = 0

    def peek(self, offset: int = 0) -> bytes | None:
        while len(self._ahead) <= offset:
            token = next(self._source, None)
            if token is None:
                return None
            self._ahead.append(token)
        return self._ahead[offset]

    def take(self) -> bytes | None:
        token = self.peek()
        if token is not None:
            self._ahead.pop(0)
            self.position += 1
        return token


def _numstat(tokens: _Tokens, sha: str) -> list[FileChange]:
    files: list[FileChange] = []
    while (token := tokens.peek()) is not None and token != b"":
        tokens.take()
        entry = _decode(token)
        if not files:
            # git separates the message from the first numstat entry with one newline.
            entry = entry.removeprefix("\n")
        match = _NUMSTAT.match(entry)
        if match is None:
            raise GitError(f"bad numstat entry in {sha}: {entry[:60]!r}")
        added, deleted, path = match.group(1), match.group(2), match.group(3)
        if path:
            files.append(FileChange(path, _count(added), _count(deleted)))
            continue
        old_path, new_path = tokens.take(), tokens.take()
        if not old_path or not new_path:
            raise GitError(f"truncated rename entry in {sha}")
        files.append(
            FileChange(_decode(new_path), _count(added), _count(deleted), _decode(old_path))
        )
    return files


def _patch_text(tokens: _Tokens) -> bytes:
    """Take the patch of one commit, putting back the NUL bytes that split it.

    Patch text ends with a newline, and a NUL inside it (from a text file whose
    first NUL lies past the 8000 bytes git checks, or one marked ``diff`` in
    ``.gitattributes``) is never right after a newline, because every patch line
    starts with a marker such as ``+``. So a token that does not end with a
    newline was cut by a NUL of the patch, and the next token continues it.
    """
    parts = [tokens.take() or b""]
    while not parts[-1].endswith(b"\n") and (more := tokens.take()) is not None:
        parts.append(more)
    return b"\0".join(parts)


def parse_log_tokens(
    tokens: Iterable[bytes],
) -> Iterator[tuple[Commit, list[FilePatch] | None]]:
    """Parse the tokens of :func:`log_command` output into commits and their file patches.

    The patches are ``None`` when the output has no patch section (``-p`` not given).
    """
    stream = _Tokens(tokens)
    marker = MARKER.encode()
    while (token := stream.peek()) is not None:
        if token == b"":
            stream.take()
            continue
        if token != marker:
            raise GitError(f"unexpected token at position {stream.position}: {token[:60]!r}")
        stream.take()
        header = [stream.take() for _ in range(4)]
        if stream.peek() is None or any(field is None for field in header):
            raise GitError("truncated commit header")
        sha, parents, date, message = (_decode(field or b"") for field in header)
        files = _numstat(stream, sha)
        patches = None
        after = stream.peek(1)
        if stream.peek() == b"" and after is not None and after.startswith(DIFF_HEADER):
            stream.take()
            try:
                patches = parse_patch(_patch_text(stream))
            except PatchError as exc:
                raise GitError(f"bad patch in {sha}: {exc}") from exc
        commit = Commit(
            sha=sha,
            parents=tuple(parents.split()),
            date=normalize_date(date),
            message=message,
            files=tuple(files),
        )
        yield commit, patches


def parse_log(data: bytes) -> Iterator[Commit]:
    """Parse complete :func:`log_command` output; patches are measured without file contents."""
    for commit, patches in parse_log_tokens(data.split(b"\0")):
        yield complete(commit, patches)
