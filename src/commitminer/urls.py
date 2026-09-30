"""Repository URLs as they may be written to exports, recordings and reports."""

from __future__ import annotations

import re
from typing import Final, overload

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
