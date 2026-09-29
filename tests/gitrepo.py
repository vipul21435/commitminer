"""Helpers that build small synthetic git repositories for the tests."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path


class GitRepo:
    """A throwaway git repository with deterministic authors and dates."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._tick = 0
        root.mkdir(parents=True, exist_ok=True)
        self.git("init", "-q", "-b", "main")

    def _env(self) -> dict[str, str]:
        date = f"2024-01-{1 + self._tick // 24:02d}T{self._tick % 24:02d}:00:00+00:00"
        return {
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "Test Author",
            "GIT_AUTHOR_EMAIL": "author@example.invalid",
            "GIT_COMMITTER_NAME": "Test Author",
            "GIT_COMMITTER_EMAIL": "author@example.invalid",
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_DATE": date,
        }

    def git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "-c", "commit.gpgsign=false", *args],
            capture_output=True,
            env=self._env(),
            check=True,
        )
        return proc.stdout.decode("utf-8", "surrogateescape")

    def write(self, files: Mapping[str, str | bytes]) -> None:
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")

    def commit(
        self,
        message: str,
        files: Mapping[str, str | bytes] | None = None,
        *,
        allow_empty: bool = False,
    ) -> str:
        """Write ``files``, stage everything and commit; return the new sha."""
        if files:
            self.write(files)
        self.git("add", "-A")
        extra = ["--allow-empty"] if allow_empty else []
        if not message:
            extra.append("--allow-empty-message")
        self.git("commit", "-q", *extra, "-m", message)
        self._tick += 1
        return self.head()

    def head(self) -> str:
        return self.git("rev-parse", "HEAD").strip()


def lines(count: int, prefix: str = "line") -> str:
    """``count`` distinct lines of text."""
    return "".join(f"{prefix} {n}\n" for n in range(count))
