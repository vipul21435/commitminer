"""Shared fixtures: small synthetic git repositories built in ``tmp_path``."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitrepo import GitRepo


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """An empty repository on branch ``main``."""
    return GitRepo(tmp_path / "repo")
