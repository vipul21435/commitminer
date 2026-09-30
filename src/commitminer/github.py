"""A small GitHub REST client: an on-disk ETag cache, rate limits and retries.

- **Conditional requests.** Every successful GET with an ``ETag`` (or
  ``Last-Modified``) is stored in :class:`ResponseCache`, keyed by its full URL.
  The next request for that URL sends ``If-None-Match`` (``If-Modified-Since``);
  a ``304 Not Modified`` answer is served from the cache. GitHub does not count
  a 304 against the rate limit of an authenticated request; without a token a
  304 costs one of the 60 requests an hour like any other answer (measured:
  three cached runs of the same three requests left 56, 53 and 50). The
  counters of a 304 are read like those of every other answer, so the
  reported budget is right either way.
- **Rate limits.** ``X-RateLimit-Remaining`` and ``X-RateLimit-Reset`` are read
  from every response; once the budget is used up, the client waits for the
  reset before the next request. A ``403`` or ``429`` answer is retried after
  its ``Retry-After`` seconds, after the reset when the budget is at zero, or
  with an exponential backoff from 60 s for a secondary rate limit without
  either header. Server errors (``5xx``) and network errors back off 1, 2, 4,
  8 s (``max_retries`` retries, 4 by default, so five attempts). A wait longer
  than ``max_wait`` (300 s by default) raises :class:`RateLimitError` instead
  of hanging: a secondary limit waits 60, 120 and 240 s and fails at the 480 s
  it would need next.
- **Injectable clock and transport.** Waiting goes through a :class:`Clock`,
  so tests advance time instead of sleeping, and requests go through any
  ``httpx`` transport, such as the record and replay transports of
  :mod:`commitminer.fixtures`.

``GITHUB_TOKEN`` is optional: without it GitHub allows 60 requests an hour,
with it 5000.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Protocol

import httpx

from commitminer import __version__

API_URL: Final = "https://api.github.com"
API_VERSION: Final = "2022-11-28"
USER_AGENT: Final = f"commitminer/{__version__}"
SECONDARY_BACKOFF: Final = 60.0
"""GitHub asks clients to wait at least a minute after a secondary rate limit."""


class GitHubError(RuntimeError):
    """The GitHub API refused a request or answered something unexpected."""


class RateLimitError(GitHubError):
    """Waiting for the rate limit would take longer than the caller allows."""


class Clock(Protocol):
    """Wall-clock time and sleeping, replaceable in tests."""

    def time(self) -> float:
        """Seconds since the epoch."""

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds``."""


class SystemClock:
    """The real clock."""

    def time(self) -> float:
        """Seconds since the epoch."""
        return time.time()

    def sleep(self, seconds: float) -> None:
        """Wait ``seconds``."""
        time.sleep(seconds)


@dataclass
class RequestStats:
    """What the client did: requests, cache hits, retries, waiting, and the last budget seen."""

    requests: int = 0
    """HTTP requests sent (retries included)."""
    not_modified: int = 0
    """Requests answered ``304 Not Modified`` and served from the cache."""
    retries: int = 0
    waited: float = 0.0
    """Seconds spent waiting for rate limits and backoffs."""
    limit: int | None = None
    remaining: int | None = None
    reset: int | None = None
    """Epoch seconds at which the budget resets."""

    def describe(self) -> str:
        """One line for the terminal."""
        requests = "1 request" if self.requests == 1 else f"{self.requests} requests"
        retries = "1 retry" if self.retries == 1 else f"{self.retries} retries"
        text = (
            f"{requests} ({self.not_modified} answered 304 from the cache), "
            f"{retries}, waited {self.waited:.0f} s"
        )
        if self.remaining is not None and self.limit is not None:
            text += f"; rate limit {self.remaining} of {self.limit} left"
            if self.reset is not None:
                text += f", resets {_utc(self.reset)}"
        return text


def _utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """A stored ``200`` answer and the validators to ask for it again."""

    url: str
    etag: str | None
    last_modified: str | None
    link: str | None
    body: str


CACHE_FORMAT: Final = "commitminer-github-cache"
CACHE_VERSION: Final = 1


class ResponseCache:
    """GET responses on disk, one JSON file per URL, written atomically.

    Entries are keyed by the SHA-256 of the full URL (query included). An
    entry that cannot be read, or belongs to another URL or format version,
    is ignored and later overwritten.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def _path(self, url: str) -> Path:
        return self.directory / f"{hashlib.sha256(url.encode()).hexdigest()[:40]}.json"

    def get(self, url: str) -> CachedResponse | None:
        """The stored response for ``url``, or ``None``."""
        try:
            raw = json.loads(self._path(url).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("format") != CACHE_FORMAT:
            return None
        if raw.get("version") != CACHE_VERSION or raw.get("url") != url:
            return None
        fields = ("etag", "last_modified", "link")
        values = [raw.get(name) for name in fields]
        body = raw.get("body")
        if not isinstance(body, str) or not all(v is None or isinstance(v, str) for v in values):
            return None
        etag, last_modified, link = values
        return CachedResponse(url, etag, last_modified, link, body)

    def put(self, entry: CachedResponse) -> None:
        """Store ``entry``, replacing any older one for its URL."""
        self.directory.mkdir(parents=True, exist_ok=True)
        record = {
            "format": CACHE_FORMAT,
            "version": CACHE_VERSION,
            "url": entry.url,
            "etag": entry.etag,
            "last_modified": entry.last_modified,
            "link": entry.link,
            "body": entry.body,
        }
        target = self._path(entry.url)
        handle, name = tempfile.mkstemp(dir=self.directory, prefix=".tmp-", suffix=".json")
        temporary = Path(name)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as stream:
                json.dump(record, stream, sort_keys=True)
            temporary.replace(target)
        except BaseException:
            with contextlib.suppress(OSError):
                temporary.unlink()
            raise


class NetworkTransport(httpx.BaseTransport):
    """The real network, reached the way a plain ``httpx.get`` would reach it.

    httpx applies ``HTTP_PROXY``, ``HTTPS_PROXY``, ``ALL_PROXY`` and
    ``NO_PROXY`` only to a client built without a transport, so a client given
    a bare ``httpx.HTTPTransport`` goes straight to the host and fails behind a
    mandatory proxy. This transport sends every request through such a
    default client (environment proxies, ``SSL_CERT_FILE``), so it can be
    wrapped by the recorder and passed to :class:`GitHubClient` like any other.
    """

    def __init__(self) -> None:
        self._client = httpx.Client()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Send ``request`` through the default client; the caller reads the stream."""
        return self._client.send(request, stream=True)

    def close(self) -> None:
        """Close the default client's connection pools."""
        self._client.close()


def default_cache_dir() -> Path:
    """``$XDG_CACHE_HOME/commitminer/github``, else ``~/.cache/commitminer/github``."""
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "commitminer" / "github"


def next_link(header: str | None) -> str | None:
    """The ``rel="next"`` URL of a ``Link`` header, or ``None``."""
    if not header:
        return None
    for part in header.split(","):
        section = part.strip()
        if not section.startswith("<") or ">" not in section:
            continue
        url, _, params = section[1:].partition(">")
        rels = {p.strip() for p in params.split(";")}
        if 'rel="next"' in rels or "rel=next" in rels:
            return url
    return None


@dataclass(frozen=True, slots=True)
class Page:
    """One decoded JSON answer and the URL of the next page, if any."""

    data: Any
    next_url: str | None


def _int_header(headers: httpx.Headers, name: str) -> int | None:
    value = headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _message(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200].strip() or "(no message)"
    if isinstance(data, dict) and isinstance(data.get("message"), str):
        return str(data["message"])
    return "(no message)"


class GitHubClient:
    """GET JSON from the GitHub REST API with caching, rate limits and retries."""

    def __init__(
        self,
        *,
        token: str | None = None,
        api_url: str = API_URL,
        cache: ResponseCache | None = None,
        transport: httpx.BaseTransport | None = None,
        clock: Clock | None = None,
        max_retries: int = 4,
        max_wait: float = 300.0,
        timeout: float = 30.0,
    ) -> None:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(
            base_url=api_url,
            headers=headers,
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
        )
        self.cache = cache
        self.clock: Clock = clock or SystemClock()
        self.max_retries = max_retries
        self.max_wait = max_wait
        self.stats = RequestStats()

    def __enter__(self) -> GitHubClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def close(self) -> None:
        """Close the connection pool."""
        self._http.close()

    def get(self, url: str, params: Mapping[str, str | int] | None = None) -> Page:
        """GET ``url`` (a path under the API URL, or an absolute URL) and decode its JSON."""
        request = self._http.build_request("GET", url, params=params)
        key = str(request.url)
        cached = self.cache.get(key) if self.cache is not None else None
        if cached is not None:
            if cached.etag:
                request.headers["If-None-Match"] = cached.etag
            if cached.last_modified:
                request.headers["If-Modified-Since"] = cached.last_modified
        response = self._send(request)
        if response.status_code == 304 and cached is not None:
            self.stats.not_modified += 1
            return Page(json.loads(cached.body), next_link(cached.link))
        if response.status_code != 200:
            raise self._error(response, key)
        body = response.text
        try:
            data = json.loads(body)
        except ValueError as exc:
            raise GitHubError(f"GET {key}: the answer is not JSON") from exc
        link = response.headers.get("link")
        etag, modified = response.headers.get("etag"), response.headers.get("last-modified")
        if self.cache is not None and (etag or modified):
            self.cache.put(CachedResponse(key, etag, modified, link, body))
        return Page(data, next_link(link))

    def pages(self, url: str, params: Mapping[str, str | int] | None = None) -> Iterator[Page]:
        """Every page of a list endpoint, following ``Link: rel="next"``."""
        page = self.get(url, params)
        yield page
        while page.next_url is not None:
            page = self.get(page.next_url)
            yield page

    def items(self, url: str, params: Mapping[str, str | int] | None = None) -> Iterator[Any]:
        """Every item of a list endpoint, fetching the next page only when needed."""
        for page in self.pages(url, params):
            if not isinstance(page.data, list):
                raise GitHubError(f"GET {url}: expected a list, got {type(page.data).__name__}")
            yield from page.data

    def _error(self, response: httpx.Response, url: str) -> GitHubError:
        status, message = response.status_code, _message(response)
        if status == 404:
            return GitHubError(
                f"not found: {url} (check OWNER/REPO; a private repository needs GITHUB_TOKEN)"
            )
        if status == 401:
            return GitHubError(f"GitHub refused the credentials (check GITHUB_TOKEN): {message}")
        return GitHubError(f"GitHub answered {status} for {url}: {message}")

    def _sleep(self, seconds: float, why: str, rate_limit: bool = True) -> None:
        """Wait ``seconds`` because of ``why``, or fail when that is longer than ``max_wait``."""
        if seconds > self.max_wait:
            hint = rate_limit and not self._http.headers.get("Authorization")
            raise RateLimitError(
                f"{why}: waiting {seconds:.0f} s is more than --max-wait {self.max_wait:g} s"
                + ("; set GITHUB_TOKEN" if hint else "")
            )
        self.clock.sleep(seconds)
        self.stats.waited += seconds

    def _wait_for_budget(self) -> None:
        if self.stats.remaining != 0 or self.stats.reset is None:
            return
        wait = self.stats.reset - self.clock.time() + 1
        if wait > 0:
            self._sleep(wait, f"rate limit used up until {_utc(self.stats.reset)}")
        self.stats.remaining = None  # unknown until the next answer

    def _note_limits(self, headers: httpx.Headers) -> None:
        limit = _int_header(headers, "x-ratelimit-limit")
        remaining = _int_header(headers, "x-ratelimit-remaining")
        reset = _int_header(headers, "x-ratelimit-reset")
        if remaining is not None:
            self.stats.limit, self.stats.remaining, self.stats.reset = limit, remaining, reset

    def _retry_delay(
        self, response: httpx.Response, attempt: int
    ) -> tuple[float, str, bool] | None:
        """How long to wait before retrying ``response``, why, and whether it is a rate limit.

        ``None`` means the answer is final.
        """
        status = response.status_code
        if status >= 500:
            return float(2**attempt), f"GitHub answered {status}", False
        if status not in (403, 429):
            return None
        retry_after = response.headers.get("retry-after")
        if retry_after is not None:
            try:
                delay = max(float(retry_after), 0.0)
            except ValueError:
                pass
            else:
                return delay, f"GitHub answered {status} with Retry-After", True
        reset = _int_header(response.headers, "x-ratelimit-reset")
        if _int_header(response.headers, "x-ratelimit-remaining") == 0 and reset is not None:
            wait = max(reset - self.clock.time(), 0.0) + 1
            return wait, f"rate limit used up until {_utc(reset)}", True
        if status == 429 or "rate limit" in _message(response).lower():
            return SECONDARY_BACKOFF * 2**attempt, "secondary rate limit", True
        return None

    def _send(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            self._wait_for_budget()
            try:
                response = self._http.send(request)
            except httpx.UnsupportedProtocol as exc:
                raise GitHubError(f"GET {request.url}: {exc}") from exc
            except httpx.TransportError as exc:
                self.stats.requests += 1
                if attempt >= self.max_retries:
                    raise GitHubError(f"GET {request.url}: {exc}") from exc
                self._sleep(float(2**attempt), f"network error: {exc}", rate_limit=False)
            except httpx.RequestError as exc:
                # Too many redirects, undecodable content: retrying cannot help.
                raise GitHubError(f"GET {request.url}: {exc}") from exc
            else:
                self.stats.requests += 1
                self._note_limits(response.headers)
                delay = self._retry_delay(response, attempt)
                if delay is None:
                    return response
                if attempt >= self.max_retries:
                    raise GitHubError(
                        f"GET {request.url}: gave up after {attempt + 1} attempts: "
                        f"{response.status_code} {_message(response)}"
                    )
                self._sleep(*delay)
            attempt += 1
            self.stats.retries += 1
