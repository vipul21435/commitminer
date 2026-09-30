"""Record GitHub API responses as fixture files and replay them offline.

Both are ``httpx`` transports, so the whole client (cache, rate limits,
retries, pagination) runs the same code on live, recorded and replayed
traffic. A fixture file holds every response recorded for one request key,
the method plus the URL path and sorted query::

    {"format": "commitminer-http-fixture", "version": 1,
     "key": "GET /repos/o/r/pulls?direction=desc&page=1&...",
     "responses": [{"status": 200, "headers": {...}, "json": ...}]}

Replay serves a key's responses in the recorded order and repeats the last
one, so a fixture can script "403 with Retry-After, then 200". A request that
carries ``If-None-Match`` equal to a recorded ``200`` answer's ``ETag`` gets
``304 Not Modified``, as GitHub would answer, so the ETag cache can be
exercised offline too.

Recordings are made to be committed and read: request headers (and so the
``Authorization`` token) are never stored, response headers are cut to the
ones the client reads, and JSON bodies to the fields CommitMiner reads
(:data:`KEPT_FIELDS`; one raw page of 100 pull requests is about 1.5 MB).
Files are pretty-printed with sorted keys.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlencode

import httpx

from commitminer.github import GitHubError

FIXTURE_FORMAT: Final = "commitminer-http-fixture"
FIXTURE_VERSION: Final = 1

KEPT_HEADERS: Final = (
    "content-type",
    "etag",
    "last-modified",
    "link",
    "location",
    "retry-after",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "x-ratelimit-resource",
    "x-ratelimit-used",
)
"""Response headers stored in fixtures: everything the client reads."""

KEPT_FIELDS: Final = frozenset(
    {
        # pull requests
        "number",
        "title",
        "body",
        "state",
        "html_url",
        "merged_at",
        "merge_commit_sha",
        "updated_at",
        "labels",
        "name",
        "base",
        "head",
        "ref",
        "sha",
        # commits
        "commit",
        "message",
        # files
        "filename",
        "previous_filename",
        "status",
        "additions",
        "deletions",
        "patch",
    }
)
"""JSON object keys kept in recorded bodies, at any depth; everything else is dropped."""


class FixtureError(GitHubError):
    """A fixture directory or file is missing, malformed, or lacks a request."""


def fixture_key(method: str, url: httpx.URL) -> str:
    """The method, path and sorted query of a request: what a fixture is looked up by."""
    query = sorted(url.params.multi_items())
    path = url.path
    return f"{method} {path}" + (f"?{urlencode(query)}" if query else "")


def fixture_name(key: str) -> str:
    """A readable, unique file name for a key."""
    slug = re.sub(r"[^a-z0-9]+", "-", key.lower()).strip("-")[:80].rstrip("-")
    return f"{slug}-{hashlib.sha256(key.encode()).hexdigest()[:10]}.json"


def trim(value: Any) -> Any:
    """``value`` with every object key outside :data:`KEPT_FIELDS` removed, recursively."""
    if isinstance(value, dict):
        return {key: trim(item) for key, item in value.items() if key in KEPT_FIELDS}
    if isinstance(value, list):
        return [trim(item) for item in value]
    return value


def _kept_headers(headers: httpx.Headers) -> dict[str, str]:
    return {name: headers[name] for name in KEPT_HEADERS if name in headers}


def _dump(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=1, sort_keys=True, ensure_ascii=True) + "\n"


class RecordingTransport(httpx.BaseTransport):
    """Send requests through ``inner`` and save every response as a fixture in ``directory``.

    The answer passed on is the recorded one (kept headers, trimmed body), so
    a recording run sees exactly what a replay of it will see.
    """

    def __init__(self, inner: httpx.BaseTransport, directory: Path) -> None:
        self._inner = inner
        self.directory = directory
        self._responses: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise FixtureError(f"{directory}: cannot create the fixture directory: {exc}") from exc

    @property
    def recorded(self) -> int:
        """Responses recorded so far."""
        return sum(len(items) for items in self._responses.values())

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Forward ``request``, store the answer, and return it with only the kept headers."""
        response = self._inner.handle_request(request)
        try:
            content = response.read()
        finally:
            response.close()
        headers = _kept_headers(response.headers)
        entry: dict[str, Any] = {"status": response.status_code, "headers": headers}
        if content:
            try:
                entry["json"] = trim(json.loads(content))
            except ValueError:
                entry["text"] = content.decode("utf-8", "replace")
            else:
                # The caller sees what a replay will serve: the trimmed body.
                content = json.dumps(entry["json"]).encode()
        key = fixture_key(request.method, request.url)
        self._responses[key].append(entry)
        record = {
            "format": FIXTURE_FORMAT,
            "version": FIXTURE_VERSION,
            "key": key,
            "responses": self._responses[key],
        }
        path = self.directory / fixture_name(key)
        try:
            path.write_text(_dump(record), encoding="ascii")
        except OSError as exc:
            raise FixtureError(f"{path}: cannot write the fixture: {exc}") from exc
        return httpx.Response(
            response.status_code, headers=headers, content=content, request=request
        )

    def close(self) -> None:
        """Close the wrapped transport."""
        self._inner.close()


def _load(path: Path) -> tuple[str, list[dict[str, Any]]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise FixtureError(f"{path}: cannot read fixture: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("format") != FIXTURE_FORMAT:
        raise FixtureError(f"{path}: not a {FIXTURE_FORMAT} file")
    if raw.get("version") != FIXTURE_VERSION:
        raise FixtureError(f"{path}: unsupported fixture version {raw.get('version')!r}")
    key, responses = raw.get("key"), raw.get("responses")
    if not isinstance(key, str) or not isinstance(responses, list) or not responses:
        raise FixtureError(f"{path}: needs a key and at least one response")
    for index, item in enumerate(responses):
        where = f"{path}: responses[{index}]"
        if not isinstance(item, dict) or not isinstance(item.get("status"), int):
            raise FixtureError(f"{where}: needs an integer status")
        headers = item.get("headers", {})
        if not isinstance(headers, dict) or not all(isinstance(v, str) for v in headers.values()):
            raise FixtureError(f"{where}: headers must map names to strings")
    return key, responses


class ReplayTransport(httpx.BaseTransport):
    """Answer requests from the fixtures in ``directory``; never touches the network."""

    def __init__(self, directory: Path) -> None:
        if not directory.is_dir():
            raise FixtureError(f"{directory}: no such fixture directory")
        self.directory = directory
        self._responses: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(directory.glob("*.json")):
            key, responses = _load(path)
            if key in self._responses:
                raise FixtureError(f"{path}: a second fixture for {key}")
            self._responses[key] = responses
        self._served: defaultdict[str, int] = defaultdict(int)

    @property
    def keys(self) -> int:
        """Recorded request keys."""
        return len(self._responses)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """The next recorded answer for ``request``'s key (or 304 when its ETag matches)."""
        key = fixture_key(request.method, request.url)
        responses = self._responses.get(key)
        if responses is None:
            raise FixtureError(f"no recorded response for {key} in {self.directory}")
        entry = responses[min(self._served[key], len(responses) - 1)]
        self._served[key] += 1
        status, headers = entry["status"], dict(entry.get("headers", {}))
        etag = headers.get("etag")
        if status == 200 and etag is not None and request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers=headers, request=request)
        if "json" in entry:
            content = json.dumps(entry["json"]).encode()
            headers.setdefault("content-type", "application/json; charset=utf-8")
        else:
            content = str(entry.get("text", "")).encode()
        return httpx.Response(status, headers=headers, content=content, request=request)
