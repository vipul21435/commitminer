"""Credentials never leave a clone's remote URL."""

from __future__ import annotations

import pytest

from commitminer.urls import strip_credentials


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://build-bot:s3cr3t@github.com/o/r.git", "https://github.com/o/r.git"),
        ("https://s3cr3t@github.com/o/r", "https://github.com/o/r"),
        ("HTTP://u:p@host:8080/o/r", "HTTP://host:8080/o/r"),
        ("https://u:p@w@host/o/r", "https://host/o/r"),  # the last @ ends the credentials
        ("https://github.com/o/r", "https://github.com/o/r"),
        ("https://github.com/o/r?a=b@c", "https://github.com/o/r?a=b@c"),
        ("https://github.com/o/r@v1", "https://github.com/o/r@v1"),
        ("ssh://git@host/o/r", "ssh://git@host/o/r"),
        ("ssh://git:s3cr3t@host/o/r", "ssh://git@host/o/r"),
        ("ssh://:s3cr3t@host/o/r", "ssh://host/o/r"),
        ("git+https://u:p@host/o/r", "git+https://host/o/r"),
        ("git@github.com:o/r.git", "git@github.com:o/r.git"),
        ("/srv/git/r.git", "/srv/git/r.git"),
        ("", ""),
    ],
)
def test_strip_credentials(url: str, expected: str) -> None:
    assert strip_credentials(url) == expected


def test_strip_credentials_passes_none_through() -> None:
    assert strip_credentials(None) is None
