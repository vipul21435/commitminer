"""Strings made safe to write into files that are handed on.

- :func:`strip_credentials`: repository URLs without ``user:password@``, for
  exports, recordings and reports.
- :func:`without_surrogates`: text without lone surrogates, for reports and
  the ledger, which store UTF-8.
"""

from __future__ import annotations

import re
from typing import Final, overload

_SURROGATE: Final = re.compile("[\ud800-\udfff]")
"""A lone surrogate: a code point that UTF-8 (and so SQLite) cannot store."""
REPLACEMENT: Final = "\ufffd"

_AUTHORITY_USER: Final = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)(?P<userinfo>[^/?#]*)@"
)
"""A URL's scheme and the ``user[:password]`` before the last ``@`` of its authority."""


@overload
def strip_credentials(url: str) -> str: ...


@overload
def strip_credentials(url: None) -> None: ...


def strip_credentials(url: str | None) -> str | None:
    """``url`` without the credentials of its authority, for files that are handed on.

    Clones are often made as ``https://<token>@host/owner/repo`` or
    ``https://user:<token>@host/...`` (CI job clones, private repositories),
    and ``remote.origin.url`` keeps that form. For ``http``, ``https`` and
    every other scheme the whole ``user[:password]@`` goes; an ``ssh://``
    URL keeps its user (``git@``), which is not a secret, and loses only a
    password. The scp form (``git@host:path``) has no password to remove.
    """
    if url is None:
        return None
    match = _AUTHORITY_USER.match(url)
    if match is None:
        return url
    scheme, userinfo = match.group("scheme"), match.group("userinfo")
    kept = ""
    if scheme.lower() == "ssh://":
        user = userinfo.split(":", 1)[0]
        kept = f"{user}@" if user else ""
    return f"{scheme}{kept}{url[match.end() :]}"


def without_surrogates(text: str) -> str:
    """``text`` with every lone surrogate replaced by U+FFFD, so it can be encoded as UTF-8.

    The walker decodes git's output with ``surrogateescape``, so a commit
    message or a path with bytes that are not UTF-8 (a Latin-1 subject from an
    old git or a repository converter) keeps them as lone surrogates such as
    ``\\udce9``. The JSON export writes them as escapes; a report file, the
    terminal and SQLite need UTF-8, and would fail on them.
    """
    return _SURROGATE.sub(REPLACEMENT, text)
