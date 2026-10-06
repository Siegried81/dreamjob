"""A per-call freshness window on top of the egress cache (``max_age``).

The cache expiry is fixed when an entry is written, from the TTL of the client
that wrote it, so a vacancy board stored under the 24-hour default stayed
"fresh" for a day.  ``EgressClient.fetch(..., max_age=...)`` lets a caller ask a
shorter question of the same cache:

* a cached body younger than ``max_age`` is served without a request;
* an older one is revalidated with a conditional GET (a 0-byte 304 when the
  board is unchanged), not downloaded again;
* a remembered 404 keeps its own negative TTL whatever ``max_age`` says, or a
  short vacancy window would re-probe every closed board on every pass;
* every vacancy adapter request carries ``max_age`` from
  ``DREAMJOB_VACANCY_CACHE_TTL_SECONDS``.

Nothing here touches the network: the real :class:`EgressClient` runs on an
``httpx.MockTransport``, and the adapter test uses a recording stub.
"""

from __future__ import annotations

import asyncio
import base64
import os
import secrets
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from dreamjob.config import get_settings
from dreamjob.db.connection import execute, query_one
from dreamjob.egress.client import DomainLimiter, EgressClient, _CacheEntry

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DEEPSEEK_API_KEY",
    "DREAMJOB_VACANCY_CACHE_TTL_SECONDS",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["DEEPSEEK_API_KEY"] = ""
    os.environ.pop("DREAMJOB_VACANCY_CACHE_TTL_SECONDS", None)
    get_settings.cache_clear()

    from dreamjob.db.migrator import migrate

    migrate()
    yield

    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Offline HTTP plumbing (same shape as test_egress_cache)
# ---------------------------------------------------------------------------

PAGE = b'{"jobs": [{"id": 1, "title": "Data Engineer"}]}'
ETAG = '"board-v1"'
URL = "https://boards.example/v1/boards/acme/jobs"


class ValidatingSite:
    """A board that answers 304 to a conditional GET carrying its current ETag."""

    def __init__(self, status: int = 200):
        self.status = status
        self.page_requests: list[httpx.Request] = []
        self.conditional_requests = 0
        self.bodies_sent = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="")
        self.page_requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, text="not found")
        if request.headers.get("if-none-match") == ETAG:
            self.conditional_requests += 1
            return httpx.Response(304, headers={"etag": ETAG})
        self.bodies_sent += 1
        return httpx.Response(
            200, content=PAGE, headers={"content-type": "application/json", "etag": ETAG}
        )


def run(site: ValidatingSite, body):
    """Run ``body(egress)`` on a real EgressClient whose socket is ``site``."""

    async def main():
        client = EgressClient()
        async with client:
            await client._client.aclose()  # noqa: SLF001 - swapping in the transport
            client._client = httpx.AsyncClient(
                transport=httpx.MockTransport(site.handler),
                headers={"User-Agent": client.settings.user_agent},
            )
            client.limiter = DomainLimiter(1000.0)  # no pacing sleeps in a unit test
            return await body(client)

    return asyncio.run(main())


def _ago(seconds: int) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def age_cache(seconds: int) -> None:
    """Pretend every cache entry was fetched ``seconds`` ago, still unexpired."""
    execute("UPDATE http_cache SET fetched_at = ?", (_ago(seconds),))


def entry(fetched_at: str | None) -> _CacheEntry:
    return _CacheEntry(
        url_hash="h", url=URL, status_code=200, headers={}, body_path=None, etag=None,
        last_modified=None, expires_at="2999-01-01T00:00:00+00:00", content_hash=None,
        raw_document_id=None, fetched_at=fetched_at,
    )


# ---------------------------------------------------------------------------
# _CacheEntry.older_than
# ---------------------------------------------------------------------------


def test_no_max_age_means_no_extra_limit() -> None:
    assert entry(None).older_than(None) is False
    assert entry(_ago(10 * 86_400)).older_than(None) is False


def test_an_entry_without_fetched_at_counts_as_old() -> None:
    """A row written before ``fetched_at`` was read cannot prove it is young."""
    assert entry(None).older_than(3600) is True
    assert entry("").older_than(3600) is True


def test_young_and_old_entries_against_max_age() -> None:
    assert entry(_ago(60)).older_than(3600) is False
    assert entry(_ago(7200)).older_than(3600) is True


# ---------------------------------------------------------------------------
# EgressClient.fetch(max_age=...)
# ---------------------------------------------------------------------------


def test_a_young_cached_body_is_served_without_a_request() -> None:
    site = ValidatingSite()

    async def twice(egress: EgressClient):
        return await egress.fetch(URL), await egress.fetch(URL, max_age=3600)

    first, second = run(site, twice)

    assert len(site.page_requests) == 1
    assert second.from_cache is True and second.content == PAGE


def test_an_old_cached_body_is_revalidated_not_served() -> None:
    """Older than ``max_age`` but inside its stored TTL: ask with a conditional GET."""
    site = ValidatingSite()
    run(site, lambda eg: eg.fetch(URL))
    age_cache(7200)

    result = run(site, lambda eg: eg.fetch(URL, max_age=3600))

    assert len(site.page_requests) == 2, "the old body must be revalidated"
    assert site.conditional_requests == 1, "with the stored validator, not a plain GET"
    assert site.bodies_sent == 1, "an unchanged board costs no second body"
    assert result.ok and result.content == PAGE
    row = query_one("SELECT fetched_at FROM http_cache WHERE url = ?", (URL,))
    assert row["fetched_at"] > _ago(60), "a 304 restarts the freshness window"


def test_without_max_age_the_same_old_body_is_still_served() -> None:
    """The general cache keeps its long TTL; only callers that ask get the short one."""
    site = ValidatingSite()
    run(site, lambda eg: eg.fetch(URL))
    age_cache(7200)

    result = run(site, lambda eg: eg.fetch(URL))

    assert len(site.page_requests) == 1
    assert result.from_cache is True


def test_a_remembered_404_ignores_max_age() -> None:
    """A closed board keeps its negative TTL, or every pass would re-probe it."""
    site = ValidatingSite(status=404)
    run(site, lambda eg: eg.fetch(URL))
    age_cache(7200)

    result = run(site, lambda eg: eg.fetch(URL, max_age=60))

    assert len(site.page_requests) == 1, "the 404 is answered from the negative cache"
    assert result.status_code == 404 and result.from_cache is True and result.negative is True


# ---------------------------------------------------------------------------
# VacancySourceAdapter._get
# ---------------------------------------------------------------------------


@dataclass
class _Response:
    url: str
    text: str = "{}"
    content: bytes = b"{}"
    status_code: int = 200
    raw_document_id: str | None = "raw-1"

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass
class RecordingEgress:
    """Answers every request with an empty 200 and records the keyword arguments."""

    calls: list[dict[str, Any]] = field(default_factory=list)

    async def fetch(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return _Response(url=url)


def _greenhouse(egress: RecordingEgress):
    from dreamjob.adapters.ats.greenhouse import GreenhouseAdapter

    return GreenhouseAdapter(egress=egress)  # type: ignore[arg-type]


def test_vacancy_requests_carry_the_vacancy_cache_ttl_as_max_age() -> None:
    egress = RecordingEgress()
    asyncio.run(_greenhouse(egress)._get(URL))

    assert egress.calls[0]["max_age"] == get_settings().vacancy_cache_ttl_seconds == 3600


def test_the_vacancy_cache_ttl_is_read_from_the_environment() -> None:
    os.environ["DREAMJOB_VACANCY_CACHE_TTL_SECONDS"] = "120"
    get_settings.cache_clear()
    egress = RecordingEgress()
    asyncio.run(_greenhouse(egress)._get(URL))

    assert egress.calls[0]["max_age"] == 120


def test_an_explicit_max_age_from_the_caller_wins() -> None:
    egress = RecordingEgress()
    asyncio.run(_greenhouse(egress)._get(URL, max_age=5))

    assert egress.calls[0]["max_age"] == 5
