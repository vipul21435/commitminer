"""Shared fixtures: small synthetic git repositories built in ``tmp_path``."""

from __future__ import annotations

from pathlib import Path

import pytest

from gitrepo import GitRepo

PROXY_VARIABLES = tuple(
    case(name)
    for name in ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
    for case in (str.lower, str.upper)
)
HOST_VARIABLES = ("GITHUB_API_URL", "GITHUB_TOKEN", "GH_TOKEN", *PROXY_VARIABLES)
"""Variables a CI runner or a shell may set that change what the GitHub commands do.

The proxy variables are among them: a proxy client often exports ``all_proxy``,
and httpx sends live requests (and fails on a proxy it cannot use) through it.
"""


@pytest.fixture(autouse=True)
def _hermetic_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without the host's GitHub settings; a test sets what it needs."""
    for name in HOST_VARIABLES:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def git_repo(tmp_path: Path) -> GitRepo:
    """An empty repository on branch ``main``."""
    return GitRepo(tmp_path / "repo")
