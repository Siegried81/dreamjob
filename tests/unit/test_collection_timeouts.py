"""One silent source may not hold the whole collection run (FR-186, NFR-401).

A bounded campaign sat at ``0/25`` pages for thirty-five minutes because one
source accepted the connection and never answered, and the page loop had no
upper bound: the job neither settled nor added data.  Three bounds now exist and
each is pinned here, separately:

* every adapter page is raced against ``PAGE_TIMEOUT_SECONDS``;
* every plan item against ``SOURCE_TIMEOUT_SECONDS``;
* the run itself against the ``STALL_SECONDS`` watchdog, plus the campaign's
  own ``max_duration_seconds`` budget, which stops it as ``deadline``.

The clock is stubbed by shrinking the module constants, so the tests measure
the *behaviour* of the bounds rather than spending them.  Nothing here touches
the network: ``EgressClient.fetch`` is replaced by a canned answer.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from dreamjob.adapters.base import (
    AdapterCapabilities,
    NormalisedRecord,
    PlanItem,
    RawRecord,
    SourceAdapter,
    SourceType,
    register_adapter,
)
from dreamjob.config import get_settings
from dreamjob.db.connection import insert_row, query_one, upsert_row, utcnow
from dreamjob.db.migrator import migrate
from dreamjob.db.repositories import campaigns as campaign_repo
from dreamjob.egress.client import EgressClient, FetchResult
from dreamjob.jobs.runner import JobContext, JobControl, runner
from dreamjob.pipeline import collection, email_validate


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DREAMJOB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("DREAMJOB_DB_PATH", str(tmp_path / "data" / "test.db"))
    get_settings.cache_clear()
    get_settings()
    migrate()
    yield
    get_settings.cache_clear()


@pytest.fixture()
def no_network(monkeypatch):
    """Every fetch answers 200 with an empty body, without leaving the process.

    ``EgressClient.fetch`` is not the only door out.  The passes that run after
    collection resolve the employer of every record collected, and that path
    reaches ``email_validate._resolve_mx`` - a real DNS query with a 5 s
    lifetime for MX and another 5 s for the A fallback.  The single record the
    fast board returns is enough: its company name becomes a guessed domain
    nothing answers for, and the run spends ~10 s of wall clock outside the
    event loop.  That is what made the bound tests below look like a collection
    bound had failed, while the page bound in the very same run fired on time at
    0.1 s.  DNS is stubbed here so these tests measure the bounds they are about
    and make no network request.
    """

    async def _fetch(self, url, **kwargs):
        return FetchResult(
            url=url, status_code=200, text="{}", content=b"{}", headers={},
            from_cache=False, raw_document_id=None, content_hash="",
        )

    monkeypatch.setattr(EgressClient, "fetch", _fetch)
    monkeypatch.setattr(
        email_validate,
        "_resolve_mx",
        lambda domain, timeout=5.0: email_validate.MXResult(
            has_mx=False, error="DNS is stubbed in tests"
        ),
    )


def _seeker() -> str:
    return insert_row(
        "job_seeker",
        {"email": "timeouts@example.com", "display_name": "Timeouts",
         "created_at": utcnow(), "updated_at": utcnow()},
    )


def _campaign(seeker_id: str, **overrides) -> str:
    directive_id = insert_row(
        "directive_set",
        {"job_seeker_id": seeker_id, "name": "Timeouts", "created_at": utcnow(),
         "job_content": {"target_titles": ["Data Engineer"]},
         "location": {"countries": ["BE"]}},
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": {}, "created_at": utcnow()},
    )
    caps = {"max_pages": 20, "max_pages_per_source": 20}
    caps.update(overrides.pop("caps", {}))
    return campaign_repo.create_campaign(
        seeker_id,
        {"name": "Timeouts", "directive_set_id": directive_id,
         "profile_version_id": profile_id, "caps": caps, **overrides},
    )


def _catalogue(adapter_key: str, **overrides) -> None:
    row = {
        "adapter_key": adapter_key,
        "display_name": adapter_key,
        "source_type": "job_board",
        "coverage_countries": ["BE"],
        "coverage_industries": [],
        "query_capabilities": AdapterCapabilities(pagination=True).__dict__,
        "access_method": "api",
        "rate_limit_rps": 10.0,
        "cost_per_call_eur": 0.0,
        "tos_status": "permitted",
        "enabled": 1,
        "requires_ack": 0,
        "updated_at": utcnow(),
    }
    row.update(overrides)
    upsert_row("source_catalogue", row, ["adapter_key"])


def _plan_item(campaign_id: str, adapter_key: str, pages: int = 1) -> str:
    return campaign_repo.insert_plan_item(
        campaign_id,
        {"adapter_key": adapter_key, "native_query": {"queries": ["data"]},
         "estimated_pages": pages, "created_at": utcnow()},
    )


def _run(campaign_id: str, seeker: str) -> str:
    """Drive the worker the way the runner does, so the terminal row is real."""
    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker)
    asyncio.run(runner._run(ctx, collection.collection_worker))
    return job_id


def _job(job_id: str) -> dict:
    return query_one("SELECT * FROM job_run WHERE id = ?", (job_id,))


def _checkpoint(job_id: str) -> dict:
    return json.loads(_job(job_id)["checkpoint"] or "{}")


def _stats(job_id: str) -> dict:
    return _checkpoint(job_id).get("stats") or {}


def _finished_detail() -> dict:
    row = query_one("SELECT detail FROM audit_event WHERE action = 'campaign.collection_finished'")
    return json.loads(row["detail"])


class _Base(SourceAdapter):
    capabilities = AdapterCapabilities(pagination=False)

    def plan(self, directives, composite_profile, caps):
        return []

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return []

    def parse(self, raw: RawRecord) -> list[dict]:
        return []

    def normalise(self, parsed: dict, raw: RawRecord) -> NormalisedRecord | None:
        return NormalisedRecord(entity_type="vacancy", data=dict(parsed))


@register_adapter
class _HangingBoard(_Base):
    """A source that accepts the page and never answers it."""

    key = "timeout.hanging"
    display_name = "Hanging Board"
    source_type = SourceType.JOB_BOARD
    API = "https://hanging.test/jobs"

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        await asyncio.sleep(3600)  # only the timeout will ever end this
        return []


@register_adapter
class _SlowBoard(_Base):
    """A source that answers every page, but slowly, forever."""

    key = "timeout.slow"
    display_name = "Slow Board"
    source_type = SourceType.JOB_BOARD
    API = "https://slow.test/jobs"

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        await asyncio.sleep(0.12)
        await self.egress.fetch(self.API)
        return []


@register_adapter
class _FastBoard(_Base):
    """A source that behaves: one page, one record, no waiting."""

    key = "timeout.fast"
    display_name = "Fast Board"
    source_type = SourceType.JOB_BOARD
    API = "https://fast.test/jobs"

    async def fetch(self, item: PlanItem) -> list[RawRecord]:
        return [RawRecord(url=self.API, content="{}", meta={
            "title": "Data Engineer", "company_name_raw": "Timeout NV", "country": "BE",
        })]

    def parse(self, raw: RawRecord) -> list[dict]:
        return [dict(raw.meta)]


# ---------------------------------------------------------------------------
# FR-186 / NFR-401: the page, the item and the run are all bounded
# ---------------------------------------------------------------------------


def test_a_source_that_never_answers_times_out_and_the_run_moves_on(db, no_network, monkeypatch):
    """The 0/25 freeze: one silent source is charged and left behind."""
    monkeypatch.setattr(collection, "PAGE_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(collection, "SOURCE_TIMEOUT_SECONDS", 5.0)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("timeout.hanging")
    _catalogue("timeout.fast")
    hanging = _plan_item(campaign_id, "timeout.hanging", pages=2)
    fast = _plan_item(campaign_id, "timeout.fast")

    before = time.monotonic()
    job_id = _run(campaign_id, seeker)
    elapsed = time.monotonic() - before

    assert elapsed < 5.0, f"the run waited {elapsed:.1f}s for a source that never answers"

    stalled = campaign_repo.get_plan_item(hanging)
    assert stalled["status"] == "failed"
    assert stalled["error_count"] == 1, "the timeout is an error on the source, not on the job"
    assert "timed out after" in stalled["last_error"], stalled["last_error"]
    assert (stalled["caps"] or {})["outcome"]["state"] == "failed"

    healthy = campaign_repo.get_plan_item(fast)
    assert healthy["status"] == "done" and healthy["records_collected"] == 1, (
        "the rest of the plan still ran"
    )
    assert _job(job_id)["status"] == "done", "a source timeout must never fail the job"
    assert _stats(job_id)["stopped_by"] is None


def test_a_source_that_keeps_answering_is_still_bounded_as_one_item(db, no_network, monkeypatch):
    """Pages each under the page limit may still not add up to an unbounded item."""
    monkeypatch.setattr(collection, "PAGE_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(collection, "SOURCE_TIMEOUT_SECONDS", 0.3)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("timeout.slow")
    item_id = _plan_item(campaign_id, "timeout.slow", pages=3)

    job_id = _run(campaign_id, seeker)

    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "failed"
    assert item["error_count"] == 1
    assert "timed out after" in item["last_error"], item["last_error"]
    assert _job(job_id)["status"] == "done", "the item's timeout is not the job's failure"


def test_the_stall_watchdog_stops_a_run_that_makes_no_progress(db, no_network, monkeypatch):
    """No page and no record for the window: the job ends, gracefully and 'done'."""
    monkeypatch.setattr(collection, "STALL_SECONDS", 0.05)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("timeout.hanging")
    item_id = _plan_item(campaign_id, "timeout.hanging", pages=2)

    before = time.monotonic()
    job_id = _run(campaign_id, seeker)
    elapsed = time.monotonic() - before

    assert elapsed < 5.0, f"the watchdog did not fire: the run took {elapsed:.1f}s"
    assert _job(job_id)["status"] == "done", "a stall stops the job; it does not fail it"
    assert _stats(job_id)["stopped_by"] == "stalled"
    assert _finished_detail()["stopped_by"] == "stalled", (
        "the terminal report carries the reason"
    )
    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "planned", "a stalled source is resumable, not failed"
    assert "stalled" in (item["last_error"] or "")
    # The terminal path published the bar before ending (the final tick).
    assert _job(job_id)["progress_total"] == 2


def test_a_deadline_stop_is_recorded_as_deadline(db, no_network):
    """The campaign's own wall-clock budget ends the run as a deadline."""
    seeker = _seeker()
    campaign_id = _campaign(seeker, caps={"max_duration_seconds": 0})
    _catalogue("timeout.fast")
    item_id = _plan_item(campaign_id, "timeout.fast")

    job_id = _run(campaign_id, seeker)

    assert _job(job_id)["status"] == "done", "a budget stop is not a failure"
    assert _stats(job_id)["stopped_by"] == "deadline"
    assert _finished_detail()["stopped_by"] == "deadline"
    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "planned", "nothing was collected, so the item stays runnable"
    assert "deadline" in (item["last_error"] or "")


def test_a_normal_run_is_unaffected_by_the_timeouts(db, no_network):
    """A source that answers within every bound behaves exactly as before."""
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("timeout.fast")
    item_id = _plan_item(campaign_id, "timeout.fast")

    job_id = _run(campaign_id, seeker)

    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "done" and item["records_collected"] == 1
    assert item["error_count"] == 0
    job = _job(job_id)
    assert job["status"] == "done"
    assert (job["progress_done"], job["progress_total"]) == (1, 1)
    assert _stats(job_id)["stopped_by"] is None
    assert "stopped_by" not in _finished_detail() or _finished_detail()["stopped_by"] is None


def test_a_pause_is_not_charged_to_the_source_timeout(db, no_network, monkeypatch):
    """Time the seeker holds the job is not the source's, however long it is."""
    monkeypatch.setattr(collection, "SOURCE_TIMEOUT_SECONDS", 0.2)
    seeker = _seeker()
    campaign_id = _campaign(seeker)
    _catalogue("timeout.fast")
    item_id = _plan_item(campaign_id, "timeout.fast")

    job_id = runner.create(collection.JOB_KIND, campaign_id=campaign_id, job_seeker_id=seeker)
    control = JobControl(paused=True)
    ctx = JobContext(job_id=job_id, campaign_id=campaign_id, job_seeker_id=seeker,
                     _control=control)

    async def run() -> None:
        async def resume() -> None:
            await asyncio.sleep(0.6)
            control.paused = False

        resume_task = asyncio.create_task(resume())
        await runner._run(ctx, collection.collection_worker)
        await resume_task

    asyncio.run(run())

    item = campaign_repo.get_plan_item(item_id)
    assert item["status"] == "done" and item["records_collected"] == 1, (
        f"the pause failed the source: {item['last_error']}"
    )
    assert item["error_count"] == 0
