"""The single egress layer for all outbound HTTP (IR-102, FR-182, FR-183).

Nothing in Dream Job calls ``httpx`` directly except this module and the mail
backends.  Routing every request through one place is what makes these
requirements testable:

* **FR-182** - robots.txt compliance *including its stated ``Crawl-delay``*,
  per-domain rate limiting with back-off, one consistent user agent, and a
  response cache with a configurable TTL so repeat campaigns do not re-fetch
  unchanged pages.  Rules that could not be read are *unknown*, not *absent*:
  the fetch is refused with :class:`RobotsUnavailable` and the failure is
  re-tried in minutes, so one bad minute cannot switch robots.txt off for a
  whole domain for a day.
* **FR-183** - every fetched page is stored raw on disk with its URL,
  timestamp and content hash, and registered in ``raw_document`` so that
  extractors can be re-run later (DR-102).
* **IR-102** - retries, logging and metrics live here rather than in each of
  the twenty-odd source adapters.

Four ways a page used to be fetched that had already been fetched (see
``docs/Data_Gathering_Plan.md`` section 4 and item N4; NFR-103 is what they
cost):

1. **The TTL expired and the whole body came back.**  ``etag`` and
   ``last_modified`` were stored and never sent.  Now they are: on expiry the
   request carries ``If-None-Match`` / ``If-Modified-Since``, and a **304**
   keeps the stored body and extends ``expires_at``.  An unchanged 3.6 MB
   Greenhouse board costs 0 bytes instead of 3.6 MB.
2. **Two plan items wanted the same URL at the same time.**  Serial collection
   hid it; domain-parallel collection (N5) would have made it N-1 duplicate
   downloads per shared page.  ``fetch`` now coalesces concurrent GETs of one
   URL onto a single in-flight future.
3. **The same page under a different spelling.**  ``?x=1&y=2`` and ``?y=2&x=1``,
   ``HOST`` and ``host``, ``https://h`` and ``https://h/`` are one resource and
   are now one cache key.  The key is computed *before* any fan-out, so
   coalescing and the cache agree on what "the same URL" means.
4. **A 404 that was probed again on every pass.**  Failures are now
   negative-cached - 404/410 for ``DREAMJOB_NEGATIVE_CACHE_TTL_SECONDS``
   (7 days), 429/5xx for six hours - and non-200 bodies are no longer written
   to the raw-document store at all: a Cloudflare challenge page differing only
   in its Ray ID is not a document (FR-183).

The **fetch ledger** at the bottom of this module is the per-target half of the
same problem (FR-342): ``http_cache`` answers "have we got this URL?", the
ledger answers "when did we last read *this board / this employer / this
query*, and how much did it give us?".  Reuse decided per adapter skipped 3,399
never-read Greenhouse boards the moment one Greenhouse board was read; decided
per target it skips exactly the targets that are still fresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import urllib.robotparser
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlsplit, urlunsplit

import httpx

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    from_json,
    insert_row,
    query_all,
    query_one,
    upsert_row,
    utcnow,
)

log = logging.getLogger(__name__)

# A robots.txt we actually read is trusted for a day; one we could not read is
# re-tried in minutes, so a single failure never disables robots.txt (FR-182).
ROBOTS_TTL_SECONDS = 86_400
ROBOTS_FAILURE_TTL_SECONDS = 300

# A resource that is gone is gone: not re-probed for a week (the default of
# DREAMJOB_NEGATIVE_CACHE_TTL_SECONDS).  A resource that is merely unwell -
# 429, 500, 502, 503, 504 - is re-probed after six hours, because "come back
# later" is exactly what those statuses mean.
GONE_STATUSES = frozenset({404, 410})
TRANSIENT_NEGATIVE_CACHE_TTL_SECONDS = 6 * 3600

# 429 and 503 are the two the limiter treats as back-pressure rather than as an
# answer (RFC 9110: both carry Retry-After).
BACKOFF_STATUSES = (429, 503)

NOT_MODIFIED = 304

# Headers worth refreshing from a 304 - the rest of the stored response is
# still the truth about the body we already hold.
_REVALIDATION_HEADERS = ("etag", "last-modified", "cache-control", "expires", "date")


class RobotsDisallowed(RuntimeError):
    """Raised when robots.txt forbids the fetch (FR-182, CR-402)."""


class RobotsUnavailable(RobotsDisallowed):
    """robots.txt could not be read, so the fetch is refused (FR-182, CR-402).

    RFC 9309 section 2.3.1.4 calls a 5xx or a transport failure *unreachable*
    and asks a crawler to assume a complete disallow.  It is a subclass of
    :class:`RobotsDisallowed` so every existing handler still catches it, and a
    distinct class so a caller can tell "the site said no" from "we never
    managed to ask".
    """


class RateLimited(RuntimeError):
    """Raised after repeated 429/503 responses from a domain."""


@dataclass
class FetchResult:
    url: str
    status_code: int
    text: str
    content: bytes
    headers: dict
    from_cache: bool
    raw_document_id: str | None
    content_hash: str
    # Served from the cache after the server answered 304 to a conditional GET:
    # a request was made, no body crossed the wire (FR-182, plan section 4 L1).
    revalidated: bool = False
    # A remembered failure: the status is real, the body is deliberately empty
    # and no raw document was stored for it (plan item N7).
    negative: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


# ---------------------------------------------------------------------------
# Cache keys (FR-182)
# ---------------------------------------------------------------------------


def normalise_url(url: str) -> str:
    """The canonical spelling of ``url`` used as a cache and coalescing key.

    One resource, one key.  Scheme and host are lower-cased, a default port is
    dropped, the fragment is dropped (it never reaches the server), an empty
    path becomes ``/`` and query parameters are sorted, so the ``?x=1&y=2`` an
    ATS adapter builds and the ``?y=2&x=1`` a crawler builds are one entry
    instead of two downloads.

    Trailing slashes *inside* a path are deliberately left alone: ``/jobs`` and
    ``/jobs/`` are different resources to plenty of servers, and inventing an
    equivalence here would serve one board's payload for another's URL.
    """
    parts = urlsplit(url.strip())
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    if port and not default_port:
        host = f"{host}:{port}"
    if parts.username:
        credentials = parts.username + (f":{parts.password}" if parts.password else "")
        host = f"{credentials}@{host}"
    path = parts.path or "/"
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return urlunsplit((scheme, host, path, query, ""))


def domain_of(url: str) -> str:
    """The rate-limit and robots.txt key for ``url``.

    Lower-cased, so ``HOST`` and ``host`` are one bucket: a limiter keyed on the
    spelling would let a mixed-case URL skip the queue, and a robots cache keyed
    on the spelling would ask the same site twice.
    """
    return urlsplit(url).netloc.lower()


def _drain_exception(future: asyncio.Future) -> None:
    """Retrieve a coalesced future's exception so asyncio does not log it twice.

    The task that owns the fetch re-raises the exception itself; every waiter
    gets it from the future.  Without this callback a future nobody waited on
    would produce a spurious "exception was never retrieved" traceback.
    """
    if not future.cancelled():
        future.exception()


#: Crawl-delays a host publishes that a strict robots.txt parser will not
#: attribute to any user-agent group, measured on the live file (FR-182).
#:
#: europa.eu states ``Crawl-delay: 10`` but puts a blank line between it and the
#: ``User-agent: *`` it plainly belongs to.  A blank line ends a record, so
#: ``urllib.robotparser`` drops the directive and ``crawl_delay()`` answers
#: ``None`` - and the EURES sweep then ran at this product's own 2 s against a
#: host asking for 10 s.  The site's stated intention is not ambiguous just
#: because its file is malformed, so the measured value is a floor here.  A
#: delay the parser *does* read still wins when it is slower.
PUBLISHED_CRAWL_DELAY_SECONDS: dict[str, float] = {
    "europa.eu": 10.0,
}


def published_crawl_delay(domain: str) -> float:
    """The measured published delay for ``domain`` or one of its parents."""
    host = (domain or "").strip().lower().rstrip(".")
    while host:
        if host in PUBLISHED_CRAWL_DELAY_SECONDS:
            return PUBLISHED_CRAWL_DELAY_SECONDS[host]
        _, _, host = host.partition(".")
    return 0.0


class DomainLimiter:
    """Per-domain token pacing with exponential back-off on 429/503.

    The pace is the *slower* of the configured rate and the domain's own
    ``Crawl-delay`` (FR-182).  europa.eu asks every client for 10 s and
    api.lever.co for 1 s; honouring that costs Campaign B 26 minutes and is the
    reason the EU's aggregator has no cause to block this product
    (``docs/Data_Gathering_Plan.md`` section 6 item 4).
    """

    def __init__(self, rps: float, *, honour_crawl_delay: bool = True):
        self.min_interval = 1.0 / rps if rps > 0 else 0.0
        self.honour_crawl_delay = honour_crawl_delay
        self._crawl_delay: dict[str, float] = {}
        self._last: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, domain: str) -> asyncio.Lock:
        if domain not in self._locks:
            self._locks[domain] = asyncio.Lock()
        return self._locks[domain]

    def set_crawl_delay(self, domain: str, delay: object) -> None:
        """Record what this domain's robots.txt asks for.  ``None`` clears it."""
        if delay is None:
            return
        try:
            seconds = float(delay)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return
        if seconds <= 0 or self._crawl_delay.get(domain) == seconds:
            return
        self._crawl_delay[domain] = seconds
        if seconds > self.min_interval:
            log.info(
                "%s asks for Crawl-delay %.1fs; pacing this domain at its rate, not ours (FR-182)",
                domain, seconds,
            )

    def interval_for(self, domain: str) -> float:
        """Seconds between two requests to ``domain``, before any penalty."""
        if not self.honour_crawl_delay:
            return self.min_interval
        return max(
            self.min_interval,
            self._crawl_delay.get(domain, 0.0),
            published_crawl_delay(domain),
        )

    async def acquire(self, domain: str) -> None:
        async with self._lock(domain):
            wait = self.interval_for(domain) + self._penalty.get(domain, 0.0)
            elapsed = time.monotonic() - self._last.get(domain, 0.0)
            if elapsed < wait:
                await asyncio.sleep(wait - elapsed)
            self._last[domain] = time.monotonic()

    def penalise(self, domain: str) -> float:
        current = self._penalty.get(domain, 0.0)
        self._penalty[domain] = min(60.0, max(2.0, current * 2))
        return self._penalty[domain]

    def relieve(self, domain: str) -> None:
        if domain in self._penalty:
            self._penalty[domain] = max(0.0, self._penalty[domain] / 2)


@dataclass
class _CacheEntry:
    """One interpreted ``http_cache`` row (FR-182)."""

    url_hash: str
    url: str
    status_code: int
    headers: dict
    body_path: Path | None
    etag: str | None
    last_modified: str | None
    expires_at: str
    content_hash: str | None
    raw_document_id: str | None
    #: When the body was last fetched or revalidated.  ``None`` on a row written
    #: before the column was read here, which :meth:`older_than` treats as old.
    fetched_at: str | None = None

    @property
    def fresh(self) -> bool:
        return self.expires_at > utcnow()

    def older_than(self, max_age: int | None) -> bool:
        """Was this entry fetched more than ``max_age`` seconds ago?

        ``expires_at`` is fixed when the entry is written, from the TTL of the
        client that wrote it, so a vacancy board stored by a 24-hour client
        would stay "fresh" for a day.  A caller that needs a shorter freshness
        window passes ``max_age`` and this check applies it on read, whatever
        TTL the entry was stored with.  ``None`` means "no extra limit".
        """
        if max_age is None:
            return False
        if not self.fetched_at:
            return True
        cutoff = (datetime.now(UTC) - timedelta(seconds=max_age)).isoformat(timespec="seconds")
        return self.fetched_at < cutoff

    @property
    def negative(self) -> bool:
        """A remembered failure rather than a stored body."""
        return not (200 <= self.status_code < 300)

    @property
    def has_body(self) -> bool:
        return self.body_path is not None and self.body_path.exists()

    @property
    def revalidatable(self) -> bool:
        """Worth a conditional GET: we still hold the body and a validator."""
        return bool(self.etag or self.last_modified) and self.has_body and not self.negative


class EgressClient:
    """Async HTTP client with caching, robots.txt and raw-document capture."""

    def __init__(
        self,
        *,
        max_concurrency: int | None = None,
        respect_robots: bool | None = None,
        cache_ttl: int | None = None,
        store_raw: bool = True,
        negative_cache_ttl: int | None = None,
    ):
        s = get_settings()
        self.settings = s
        self.store_raw = store_raw
        self.respect_robots = s.respect_robots if respect_robots is None else respect_robots
        self.cache_ttl = s.http_cache_ttl_seconds if cache_ttl is None else cache_ttl
        self.negative_cache_ttl = (
            s.negative_cache_ttl_seconds if negative_cache_ttl is None else negative_cache_ttl
        )
        self.limiter = DomainLimiter(s.per_domain_rps, honour_crawl_delay=s.honour_crawl_delay)
        self._sem = asyncio.Semaphore(max_concurrency or s.http_max_concurrency)
        self._robots: dict[str, urllib.robotparser.RobotFileParser] = {}
        # Why a domain has no rules, when the answer is "we could not ask".
        self._robots_failures: dict[str, str] = {}
        # url_hash -> the fetch already running for it (single flight).
        self._inflight: dict[str, asyncio.Future] = {}
        self._client: httpx.AsyncClient | None = None
        self.stats = {"fetched": 0, "cached": 0, "errors": 0, "blocked": 0}
        # What the cache saved, kept apart from ``stats`` because callers read
        # ``stats`` as "requests this run made" (collection._egress_calls).
        self.savings = {
            "coalesced": 0,
            "revalidated": 0,
            "not_modified": 0,
            "negative_hits": 0,
            "negative_stored": 0,
            "bytes_not_transferred": 0,
        }

    async def __aenter__(self) -> EgressClient:
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
            headers={
                "User-Agent": self.settings.user_agent,
                "Accept-Language": "en,nl;q=0.8,fr;q=0.7",
            },
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # -- robots.txt (FR-182, CR-402) ---------------------------------------
    def _client_or_raise(self) -> httpx.AsyncClient:
        """The open httpx client, or a loud error naming the misuse.

        This used to be a bare ``assert`` inside the robots.txt ``try``, so
        using the client outside its ``async with`` block was silently recorded
        as "this domain publishes no rules" for a day (FR-182).
        """
        if self._client is None:
            raise RuntimeError("EgressClient must be used as an async context manager")
        return self._client

    async def _read_robots(self, scheme: str, domain: str) -> tuple[str | None, int, str]:
        """Fetch robots.txt.  Returns ``(body_or_None, ttl_seconds, reason)``.

        ``None`` for the body means *unreachable* - RFC 9309 section 2.3.1.4 -
        and is stored as SQL NULL so it is never mistaken for an empty (i.e.
        permissive) robots.txt.  Unreachable is cached only for minutes, so a
        transient outage cannot switch robots.txt off for a whole day.
        """
        url = f"{scheme or 'https'}://{domain}/robots.txt"
        try:
            resp = await self._client_or_raise().get(url, timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - transport failure is "unreachable"
            return None, ROBOTS_FAILURE_TTL_SECONDS, f"{type(exc).__name__}: {exc}"
        if resp.status_code == 200:
            return resp.text, ROBOTS_TTL_SECONDS, ""
        if resp.status_code == 429 or resp.status_code >= 500:
            # A transient refusal or an outage is "come back later", not "this
            # site publishes no rules".  Caching 429 as an empty robots.txt
            # switched the gate off for the domain for a whole day.
            return None, ROBOTS_FAILURE_TTL_SECONDS, f"robots.txt returned HTTP {resp.status_code}"
        if 400 <= resp.status_code < 500:
            # RFC 9309: "unavailable" - the site publishes no rules.
            return "", ROBOTS_TTL_SECONDS, ""
        return None, ROBOTS_FAILURE_TTL_SECONDS, f"robots.txt returned HTTP {resp.status_code}"

    async def _robots_for(self, url: str) -> urllib.robotparser.RobotFileParser | None:
        """The parsed rules for this domain, or ``None`` when they are unknown."""
        parsed = urlparse(url)
        domain = domain_of(url)
        if domain in self._robots:
            return self._robots[domain]

        cached = await asyncio.to_thread(
            query_one,
            "SELECT body, expires_at FROM robots_cache WHERE domain = ?",
            (domain,),
        )
        if cached and cached["expires_at"] > utcnow():
            if cached["body"] is None:
                # A previous attempt could not read the rules and the retry
                # window has not elapsed yet: still unknown, still refused.
                self._robots_failures[domain] = "robots.txt is still unreachable (cached)"
                return None
            return self._memoise_robots(domain, cached["body"])

        body, ttl, reason = await self._read_robots(parsed.scheme, domain)
        await asyncio.to_thread(
            upsert_row,
            "robots_cache",
            {
                "domain": domain,
                "body": body,
                "fetched_at": utcnow(),
                "expires_at": (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(
                    timespec="seconds"
                ),
            },
            ["domain"],
        )
        if body is None:
            log.warning(
                "robots.txt for %s could not be read (%s); refusing the fetch (FR-182)",
                domain, reason,
            )
            self._robots_failures[domain] = reason
            return None
        return self._memoise_robots(domain, body)

    def _memoise_robots(self, domain: str, body: str) -> urllib.robotparser.RobotFileParser:
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
        self._robots[domain] = rp
        self._robots_failures.pop(domain, None)
        # FR-182: robots.txt says *how fast* as well as *whether*.  Reading the
        # rules and ignoring the delay was the one part of the file we skipped.
        try:
            self.limiter.set_crawl_delay(domain, rp.crawl_delay(self.settings.user_agent))
        except Exception:  # noqa: BLE001 - a malformed directive must not stop a fetch
            log.debug("Could not read a Crawl-delay from %s/robots.txt", domain, exc_info=True)
        return rp

    async def allowed(self, url: str) -> bool:
        """True when robots.txt permits this fetch.

        Unknown is not permission: a domain whose robots.txt could not be read
        answers False, and :meth:`fetch` turns that into
        :class:`RobotsUnavailable` rather than silently crawling on (FR-182).
        """
        if not self.respect_robots:
            return True
        rp = await self._robots_for(url)
        if rp is None:
            return False
        return rp.can_fetch(self.settings.user_agent, url)

    # -- cache (FR-182) -----------------------------------------------------
    @staticmethod
    def _url_hash(url: str) -> str:
        """The cache key: a hash of the *normalised* URL, never of the spelling."""
        return hashlib.sha256(normalise_url(url).encode()).hexdigest()

    @classmethod
    def _cache_key(
        cls,
        url: str,
        headers: object = None,
        *,
        method: str = "GET",
        body: object = None,
        params: object = None,
    ) -> str:
        """The cache and single-flight key for one *representation* of a URL.

        A key on the URL alone answers a request for the PDF with the JSON body
        that was stored an hour earlier: the NBB gateway serves a filing at one
        route and distinguishes the two by ``Accept``, so the structured-first,
        PDF-second fallback (FR-242) used to get JSON both times inside the TTL.
        Coalescing would make that concurrent as well as cached, so the
        representation belongs in the key.

        A POST is keyed on its body too.  The EUREs search is a POST to one
        route with a different body per query; keying those on the URL alone
        would answer one search with another's results, and not caching them at
        all re-ran every sweep (FR-342, plan item N7).

        Only an explicit ``Accept`` counts, so every caller that sends none -
        which is nearly all of them - keeps the key it already had.
        """
        parts = [normalise_url(url)]
        accept = ""
        if isinstance(headers, dict):
            accept = next(
                (str(v) for k, v in headers.items() if str(k).lower() == "accept"), ""
            ).strip()
        if accept and accept != "*/*":
            parts.append(accept)
        if params:
            # Query parameters select a different representation too.  Nothing
            # cached passes them today, but a key that ignores them would answer
            # one parameter set with another's body the moment something does.
            try:
                parts.append(json.dumps(params, sort_keys=True, separators=(",", ":")))
            except (TypeError, ValueError):
                parts.append(str(sorted(str(item) for item in params)))
        if (method or "GET").upper() != "GET":
            parts.append((method or "GET").upper())
            if body is not None:
                try:
                    parts.append(json.dumps(body, sort_keys=True, separators=(",", ":")))
                except (TypeError, ValueError):
                    parts.append(str(body))
        return hashlib.sha256("\n".join(parts).encode()).hexdigest()

    def _cache_entry(self, key: str) -> _CacheEntry | None:
        row = query_one("SELECT * FROM http_cache WHERE url_hash = ?", (key,))
        if not row:
            return None
        headers = from_json(row.get("headers"), {}) or {}
        body_path = Path(row["body_path"]) if row.get("body_path") else None
        return _CacheEntry(
            url_hash=key,
            url=row.get("url") or "",
            status_code=int(row["status_code"] or 200),
            headers=headers if isinstance(headers, dict) else {},
            body_path=body_path,
            etag=row.get("etag"),
            last_modified=row.get("last_modified"),
            expires_at=row["expires_at"],
            # Columns added by migration 090; ``.get`` keeps an older database
            # readable rather than crashing every fetch on it.
            content_hash=row.get("content_hash"),
            raw_document_id=row.get("raw_document_id"),
            fetched_at=row.get("fetched_at"),
        )

    def _serve(self, entry: _CacheEntry, url: str) -> FetchResult | None:
        """Answer from a fresh cache entry, or ``None`` if it cannot be served.

        A negative entry answers with its status and no body - that *is* the
        answer, and the point of it is that no request is made (item N7).  A
        429/503 that we already gave up on is re-raised as :class:`RateLimited`
        so the caller sees the same thing it saw the first time.
        """
        if entry.negative:
            self.stats["cached"] += 1
            self.savings["negative_hits"] += 1
            log.debug("Not re-probing %s: HTTP %s is remembered until %s",
                      url, entry.status_code, entry.expires_at)
            if entry.status_code in BACKOFF_STATUSES:
                raise RateLimited(
                    f"{domain_of(url)} returned {entry.status_code}; "
                    f"not re-probed before {entry.expires_at}"
                )
            return FetchResult(
                url=url,
                status_code=entry.status_code,
                text="",
                content=b"",
                headers=dict(entry.headers),
                from_cache=True,
                raw_document_id=None,
                content_hash="",
                negative=True,
            )
        content = self._read_body(entry)
        if content is None:
            return None
        self.stats["cached"] += 1
        return self._result_from(entry, url, content)

    @staticmethod
    def _read_body(entry: _CacheEntry) -> bytes | None:
        """The stored body, or ``None`` when it is not on disk any more.

        The pruner (item N7) and an operator with a full disk can both remove a
        file the cache row still points at, and a missing file must degrade into
        one more fetch rather than into an empty page or a traceback.
        """
        if entry.body_path is None:
            return None
        try:
            return entry.body_path.read_bytes()
        except OSError as exc:
            log.info("Cached body for %s is unreadable (%s); fetching it again", entry.url, exc)
            return None

    def _result_from(
        self, entry: _CacheEntry, url: str, content: bytes, *, revalidated: bool = False
    ) -> FetchResult:
        # FR-183/FR-166: the cached body *is* a stored raw document - it is the
        # file the raw_document row points at - so a cache hit must carry the
        # same provenance link a live fetch does.  Migration 090 stores both, so
        # a hit no longer re-hashes a 3.6 MB body to find out which one it is.
        content_hash = entry.content_hash or hashlib.sha256(content).hexdigest()
        doc_id = entry.raw_document_id or self._raw_document_id_for(content_hash)
        return FetchResult(
            url=url,
            status_code=entry.status_code,
            text=content.decode("utf-8", errors="replace"),
            content=content,
            headers=dict(entry.headers),
            from_cache=True,
            raw_document_id=doc_id,
            content_hash=content_hash,
            revalidated=revalidated,
        )

    def _remember(
        self, key: str, url: str, resp: httpx.Response, doc_id: str, content_hash: str
    ) -> None:
        """Store a 200 in ``http_cache`` with its body, validators and provenance."""
        doc = query_one("SELECT storage_path FROM raw_document WHERE id = ?", (doc_id,))
        if not doc:
            return
        upsert_row(
            "http_cache",
            {
                "url_hash": key,
                "url": url,
                "status_code": resp.status_code,
                "headers": dict(resp.headers),
                "body_path": str(self.settings.abs_data_dir / doc["storage_path"]),
                "etag": resp.headers.get("etag"),
                "last_modified": resp.headers.get("last-modified"),
                "content_hash": content_hash,
                "raw_document_id": doc_id,
                "fetched_at": utcnow(),
                "expires_at": self._expiry(self.cache_ttl),
            },
            ["url_hash"],
        )

    def _remember_failure(
        self, key: str, url: str, status: int, keep: _CacheEntry | None = None
    ) -> None:
        """Negative-cache a failure so it is not re-probed (plan item N7).

        Up to four RSS-feed probes per company times 7,500 companies is 30,000
        requests per enrichment pass, almost all of them 404s that were 404s
        last time too.  A row with no body and no raw document is the whole
        mechanism: :meth:`_serve` answers from it without touching the network.

        ``keep`` is the entry a revalidation was about to refresh.  A *transient*
        failure must not throw away a body and its validators that are still
        good: when the origin recovers, revalidating costs one 0-byte 304 rather
        than a full download.  The failure itself is still returned to the
        caller, so a source that stopped answering stays visible as a failure
        (``docs/Data_Gathering_Plan.md`` section 3 step 8).  A 404 or 410 does
        overwrite it: the resource is gone, and the body will never be current
        again.
        """
        ttl = self.negative_ttl(status)
        if ttl <= 0:
            return
        if keep is not None and keep.has_body and status not in GONE_STATUSES:
            log.debug(
                "%s answered %s while revalidating; keeping the stored body and its validators",
                url, status,
            )
            return
        upsert_row(
            "http_cache",
            {
                "url_hash": key,
                "url": url,
                "status_code": status,
                "headers": {},
                "body_path": None,
                "etag": None,
                "last_modified": None,
                "content_hash": None,
                "raw_document_id": None,
                "fetched_at": utcnow(),
                "expires_at": self._expiry(ttl),
            },
            ["url_hash"],
        )
        self.savings["negative_stored"] += 1
        log.debug("Remembering HTTP %s for %s for %ss", status, url, ttl)

    def negative_ttl(self, status: int) -> int:
        """How long a failure is believed.  0 means "ask again next time".

        401/403 are deliberately not cached: a refusal is often a transient bot
        block that a later pass gets past, and believing it for hours would skip
        a site that has become readable (test_egress_cache).
        """
        if status in GONE_STATUSES:
            return max(0, int(self.negative_cache_ttl))
        if status == 429 or 500 <= status < 600:
            return TRANSIENT_NEGATIVE_CACHE_TTL_SECONDS
        return 0

    @staticmethod
    def _expiry(ttl: int) -> str:
        return (datetime.now(UTC) + timedelta(seconds=ttl)).isoformat(timespec="seconds")

    @staticmethod
    def _raw_document_id_for(content_hash: str) -> str | None:
        row = query_one("SELECT id FROM raw_document WHERE content_hash = ?", (content_hash,))
        return str(row["id"]) if row else None

    # -- conditional GET (FR-182, plan section 4 L1) ------------------------
    @staticmethod
    def _with_validators(kwargs: dict, entry: _CacheEntry) -> dict:
        """Add ``If-None-Match`` / ``If-Modified-Since`` for a stale entry."""
        headers = dict(kwargs.get("headers") or {})  # type: ignore[arg-type]
        if entry.etag:
            headers["If-None-Match"] = entry.etag
        if entry.last_modified:
            headers["If-Modified-Since"] = entry.last_modified
        return {**kwargs, "headers": headers}

    @staticmethod
    def _without_validators(kwargs: dict) -> dict:
        """Drop the conditional headers again, for the unconditional retry."""
        headers = {
            k: v
            for k, v in (kwargs.get("headers") or {}).items()
            if k not in ("If-None-Match", "If-Modified-Since")
        }
        out = dict(kwargs)
        if headers:
            out["headers"] = headers
        else:
            out.pop("headers", None)
        return out

    def _not_modified(self, entry: _CacheEntry, resp: httpx.Response) -> FetchResult | None:
        """Serve a 304: keep the body, refresh the validators, extend the TTL.

        Returns ``None`` when the stored body has vanished from disk under us,
        which leaves the caller to re-ask unconditionally rather than hand back
        an empty page.
        """
        content = self._read_body(entry)
        if content is None:
            return None
        headers = dict(entry.headers)
        for name in _REVALIDATION_HEADERS:
            value = resp.headers.get(name)
            if value:
                headers[name] = value
        content_hash = entry.content_hash or hashlib.sha256(content).hexdigest()
        doc_id = entry.raw_document_id or self._raw_document_id_for(content_hash)
        upsert_row(
            "http_cache",
            {
                "url_hash": entry.url_hash,
                "url": entry.url,
                "status_code": entry.status_code,
                "headers": headers,
                "body_path": str(entry.body_path),
                "etag": resp.headers.get("etag") or entry.etag,
                "last_modified": resp.headers.get("last-modified") or entry.last_modified,
                "content_hash": content_hash,
                "raw_document_id": doc_id,
                "fetched_at": utcnow(),
                "expires_at": self._expiry(self.cache_ttl),
            },
            ["url_hash"],
        )
        self.savings["revalidated"] += 1
        self.savings["not_modified"] += 1
        self.savings["bytes_not_transferred"] += len(content)
        refreshed = _CacheEntry(**{**entry.__dict__, "headers": headers,
                                   "content_hash": content_hash, "raw_document_id": doc_id})
        return self._result_from(refreshed, entry.url or str(resp.url), content, revalidated=True)

    # -- raw document store (FR-183, DR-102) --------------------------------
    def _store_raw(
        self, url: str, content: bytes, content_type: str, status: int, access_method: str
    ) -> tuple[str | None, str]:
        content_hash = hashlib.sha256(content).hexdigest()
        if not self.store_raw:
            return None, content_hash
        if not 200 <= status < 300:
            # FR-183 stores the *page*, and an error page is not one: 33 of 52
            # rows in the live store were non-200 bodies, eight of them the same
            # Cloudflare challenge differing only in its Ray ID.  The status is
            # still returned to the caller and remembered in http_cache; it is
            # only the body that stops being archived (plan item N7).
            return None, content_hash

        existing = query_one("SELECT id FROM raw_document WHERE content_hash = ?", (content_hash,))
        if existing:
            return existing["id"], content_hash

        shard = self.settings.raw_dir / content_hash[:2] / content_hash[2:4]
        shard.mkdir(parents=True, exist_ok=True)
        ext = ".pdf" if "pdf" in content_type else ".html" if "html" in content_type else ".bin"
        path = shard / f"{content_hash}{ext}"
        path.write_bytes(content)

        doc_id = insert_row(
            "raw_document",
            {
                "url": url,
                "content_type": content_type,
                "content_hash": content_hash,
                "storage_path": str(path.relative_to(self.settings.abs_data_dir)),
                "byte_size": len(content),
                "http_status": status,
                "access_method": access_method,
                "fetched_at": utcnow(),
            },
        )
        return doc_id, content_hash

    # -- fetch --------------------------------------------------------------
    async def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        use_cache: bool = True,
        access_method: str = "http",
        max_retries: int = 3,
        respect_robots: bool | None = None,
        max_age: int | None = None,
        **kwargs: object,
    ) -> FetchResult:
        """Fetch ``url`` once - however many callers ask for it at once.

        The cache key is computed first, from the normalised URL, so the
        single-flight map and ``http_cache`` agree on identity before anything
        fans out (FR-182).  Concurrent GETs of one URL share one response; the
        second caller costs no request, no rate-limit slot and no bytes.

        ``max_age`` caps how old a cached body may be before it is revalidated,
        on top of the stored expiry (see :meth:`_CacheEntry.older_than`).  It is
        how a vacancy source gets a short freshness window while registries and
        filings keep the long default: same cache, a different question per
        caller.  A revalidation of an unchanged board is a 304 and costs no body.
        """
        # A GET, and a read-like POST (a search endpoint), are both cacheable;
        # the key carries the body so two searches are two representations.
        method_upper = (method or "GET").upper()
        if not (use_cache and method_upper in ("GET", "POST")):
            return await self._fetch_live(
                url, key=None, conditional=None, method=method,
                access_method=access_method, max_retries=max_retries,
                respect_robots=respect_robots, **kwargs,
            )

        body = kwargs.get("json")
        if body is None:
            body = kwargs.get("data") if kwargs.get("data") is not None else kwargs.get("content")
        key = self._cache_key(
            url,
            kwargs.get("headers"),
            method=method_upper,
            body=body,
            params=kwargs.get("params"),
        )
        pending = self._inflight.get(key)
        if pending is not None:
            self.savings["coalesced"] += 1
            return await pending

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_drain_exception)
        self._inflight[key] = future
        try:
            result = await self._fetch_cached(
                url, key, method=method_upper, access_method=access_method,
                max_retries=max_retries, respect_robots=respect_robots,
                max_age=max_age, **kwargs
            )
        except BaseException as exc:
            future.set_exception(exc)
            raise
        else:
            future.set_result(result)
            return result
        finally:
            self._inflight.pop(key, None)

    async def _fetch_cached(
        self,
        url: str,
        key: str,
        *,
        method: str = "GET",
        access_method: str,
        max_retries: int,
        max_age: int | None = None,
        **kwargs: object,
    ) -> FetchResult:
        entry = self._cache_entry(key)
        # ``max_age`` only shortens the life of a stored body.  A remembered
        # 404 keeps its own negative TTL, or a short vacancy window would
        # re-probe every closed board on every pass.
        if entry and entry.fresh and (entry.negative or not entry.older_than(max_age)):
            served = self._serve(entry, url)
            if served is not None:
                return served
        # Expired but still holding the body and a validator: ask whether it
        # changed instead of downloading it again.  A POST has no validators to
        # revalidate with, so it simply runs again.
        conditional = entry if (entry is not None and entry.revalidatable) else None
        # A cached read is an ordinary crawl request, so the robots gate applies.
        return await self._fetch_live(
            url, key=key, conditional=conditional, method=method,
            access_method=access_method, max_retries=max_retries, **kwargs,
        )

    async def _fetch_live(
        self,
        url: str,
        *,
        key: str | None,
        conditional: _CacheEntry | None,
        method: str,
        access_method: str,
        max_retries: int,
        respect_robots: bool | None = None,
        **kwargs: object,
    ) -> FetchResult:
        client = self._client_or_raise()
        domain = domain_of(url)
        # A caller may waive the robots gate for a request that is not a crawl:
        # probing whether a host exists reads nothing of the site, and gating it
        # on robots.txt rejected live sites whose robots file was merely
        # unreachable - which lost real company domains.
        robots_ok = self.respect_robots if respect_robots is None else respect_robots
        if robots_ok and not await self.allowed(url):
            self.stats["blocked"] += 1
            reason = self._robots_failures.get(domain)
            if reason:
                raise RobotsUnavailable(f"robots.txt for {domain} could not be read: {reason}")
            raise RobotsDisallowed(f"robots.txt disallows {url}")

        request_kwargs = dict(kwargs)
        if conditional is not None:
            request_kwargs = self._with_validators(request_kwargs, conditional)

        async with self._sem:
            for attempt in range(max_retries):
                await self.limiter.acquire(domain)
                try:
                    resp = await client.request(method, url, **request_kwargs)  # type: ignore[arg-type]
                except httpx.HTTPError as exc:
                    if attempt == max_retries - 1:
                        self.stats["errors"] += 1
                        raise
                    log.debug("Retrying %s after %s", url, exc)
                    await asyncio.sleep(2 ** attempt)
                    continue

                if resp.status_code in BACKOFF_STATUSES:
                    delay = self.limiter.penalise(domain)
                    log.info("Rate-limited by %s; backing off %.1fs", domain, delay)
                    if attempt == max_retries - 1:
                        if key is not None:
                            self._remember_failure(key, url, resp.status_code, conditional)
                        raise RateLimited(f"{domain} returned {resp.status_code} repeatedly")
                    await asyncio.sleep(delay)
                    continue

                self.limiter.relieve(domain)
                self.stats["fetched"] += 1

                if conditional is not None and resp.status_code == NOT_MODIFIED:
                    unchanged = self._not_modified(conditional, resp)
                    if unchanged is not None:
                        return unchanged
                    # The stored body is gone; ask again without the validators.
                    request_kwargs = self._without_validators(request_kwargs)
                    conditional = None
                    continue

                content = resp.content
                ctype = resp.headers.get("content-type", "")
                doc_id, chash = self._store_raw(
                    url, content, ctype, resp.status_code, access_method
                )

                if key is not None:
                    if resp.status_code == 200 and doc_id:
                        self._remember(key, url, resp, doc_id, chash)
                    elif resp.status_code != 200:
                        self._remember_failure(key, url, resp.status_code, conditional)

                return FetchResult(
                    url=str(resp.url),
                    status_code=resp.status_code,
                    text=resp.text,
                    content=content,
                    headers=dict(resp.headers),
                    from_cache=False,
                    raw_document_id=doc_id,
                    content_hash=chash,
                )

        raise RateLimited(f"Gave up fetching {url} after {max_retries} attempts")

    async def fetch_json(self, url: str, **kwargs: object) -> object:
        result = await self.fetch(url, **kwargs)
        import json

        return json.loads(result.text)

    async def fetch_many(self, urls: list[str], **kwargs: object) -> list[FetchResult | Exception]:
        """Fetch concurrently; failures come back as exceptions, not raises.

        Duplicates in ``urls`` - two plan items naming one careers page - are
        one request: :meth:`fetch` coalesces them (plan item N5).
        """
        tasks = [self.fetch(u, **kwargs) for u in urls]
        return await asyncio.gather(*tasks, return_exceptions=True)  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# The fetch ledger (FR-342, plan item N4)
# ---------------------------------------------------------------------------


@dataclass
class LedgerEntry:
    """What the last read of one target produced."""

    adapter_key: str
    target_key: str
    url: str
    fetched_at: str
    http_status: int | None = None
    record_count: int = 0
    etag: str | None = None
    last_modified: str | None = None

    @classmethod
    def from_row(cls, row: dict) -> LedgerEntry:
        return cls(
            adapter_key=row["adapter_key"],
            target_key=row["target_key"],
            url=row["url"],
            fetched_at=row["fetched_at"],
            http_status=row.get("http_status"),
            record_count=int(row.get("record_count") or 0),
            etag=row.get("etag"),
            last_modified=row.get("last_modified"),
        )


def record_fetch(
    adapter_key: str,
    target_key: str,
    url: str,
    *,
    http_status: int | None = None,
    record_count: int = 0,
    etag: str | None = None,
    last_modified: str | None = None,
    fetched_at: str | None = None,
) -> None:
    """Write down that this target was read (FR-342, FR-183).

    Called by the collection worker once per plan item, whatever the item ended
    as - a 404 board is as worth remembering as a 200 one, because both answer
    "do not read this again this week".  The row is keyed on
    ``(adapter_key, target_key, url)``, so a paginated target keeps one row per
    page and :func:`last_fetch` still answers for the target as a whole.
    """
    upsert_row(
        "fetch_ledger",
        {
            "adapter_key": adapter_key,
            "target_key": str(target_key),
            "url": url,
            "etag": etag,
            "last_modified": last_modified,
            "fetched_at": fetched_at or utcnow(),
            "http_status": http_status,
            "record_count": int(record_count or 0),
        },
        ["adapter_key", "target_key", "url"],
    )


def last_fetch(adapter_key: str, target_key: str) -> LedgerEntry | None:
    """The most recent read of one target, or ``None`` if it was never read."""
    row = query_one(
        "SELECT * FROM fetch_ledger WHERE adapter_key = ? AND target_key = ? "
        "ORDER BY fetched_at DESC LIMIT 1",
        (adapter_key, str(target_key)),
    )
    return LedgerEntry.from_row(row) if row else None


def _cutoff(staleness_days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=staleness_days)).isoformat(timespec="seconds")


def target_is_fresh(adapter_key: str, target_key: str, staleness_days: float) -> bool:
    """Was this target read inside the staleness window?

    This is the question ``assess_reuse`` used to answer per *adapter*: one
    Greenhouse board read on Monday marked all 3,400 of them fresh until the
    following Monday, so the second campaign skipped boards it had never seen.
    """
    row = query_one(
        "SELECT 1 AS hit FROM fetch_ledger "
        "WHERE adapter_key = ? AND target_key = ? AND fetched_at >= ? LIMIT 1",
        (adapter_key, str(target_key), _cutoff(staleness_days)),
    )
    return row is not None


def fresh_targets(adapter_key: str, staleness_days: float) -> set[str]:
    """Every target of this adapter read inside the window, in one query.

    A campaign plans thousands of items; asking per item would be thousands of
    round trips, so the planner asks once per adapter and filters in memory.
    """
    rows = query_all(
        "SELECT DISTINCT target_key FROM fetch_ledger WHERE adapter_key = ? AND fetched_at >= ?",
        (adapter_key, _cutoff(staleness_days)),
    )
    return {r["target_key"] for r in rows}
