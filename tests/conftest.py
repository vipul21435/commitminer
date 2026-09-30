"""Shared fixtures: small synthetic git repositories built in ``tmp_path``."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitrepo import GitRepo

HOST_VARIABLES = ("GITHUB_API_URL", "GITHUB_TOKEN", "GH_TOKEN")
"""Variables a CI runner or a shell may set that change what the GitHub commands do."""


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without the host's GitHub settings; a test sets what it needs."""
    for name in HOST_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """An empty repository on branch ``main``."""
    return GitRepo(tmp_path / "repo")
