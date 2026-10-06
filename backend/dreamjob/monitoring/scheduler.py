"""The periodic loop (FR-401, FR-403, FR-327, FR-364, NFR-303).

Five recurring pieces of work, none of which belongs to a request:

============================  ==========  ==================================
task                          default     what it does
============================  ==========  ==================================
``vacancy_refresh``           10 minutes  re-reads the ATS boards behind the
                                          ranked lists and adds new postings
                                          (``DREAMJOB_VACANCY_REFRESH_SECONDS``)
``job_alerts``                15 minutes  reads the LinkedIn, ictjob.be,
                                          Glassdoor and Jobat alert e-mails
                                          in connected mailboxes; a Jobat
                                          alert carries no posting link, so
                                          its rows are leads to look up
                                          (``DREAMJOB_JOB_ALERTS_SECONDS``)
``watchlist``                 hourly      runs the watches that are due
                                          (FR-401); each watch's own
                                          interval decides which those are
``replies``                   15 minutes  classifies replies the mail slice
                                          has stored (FR-422)
``digest``                    6 hours     generates the digests that are
                                          due; each seeker gets one a week
                                          (FR-403)
``follow_ups``                hourly      notifies about follow-ups whose
                                          date has passed (FR-327)
``retention``                 daily       deletes third-party records past
                                          their retention date (NFR-303)
``llm_redaction``             daily       nulls prompt and response text
                                          past its retention (FR-364)
============================  ==========  ==================================

Two properties make this safe to run inside the API process:

*Nothing here sends anything.*  The scheduler classifies, drafts, notifies and
deletes.  Every outbound message still waits for the job seeker (NFR-305).

*Everything is idempotent and time-stamped.*  Each task records its last run
in ``app_setting``, so a restart does not re-run the day's work, and every
task's own writes are de-duplicated (a watch has ``next_check_at``, a
notification has ``dedup_key``, a digest has its period as a key).  A task
that raises is logged and retried on its next tick; one broken task never
stops the others.

Running it for real
-------------------

In development the loop lives in the API process.  :func:`maybe_autostart`
starts it from the application lifespan, controlled by
``DREAMJOB_SCHEDULER_ENABLED`` (default on) with
``DREAMJOB_SCHEDULER_AUTOSTART`` still honoured as an explicit override.  An
administrator can also start and stop it from ``POST
/api/monitoring/scheduler/start``.  It used to be reachable only from that API
call, so nothing ever ran.

For a real deployment on macOS, run the tasks from ``launchd`` instead, so
that they survive an API restart and appear in the system log::

    # ~/Library/LaunchAgents/net.stepvda.dreamjob.monitoring.plist
    <?xml version="1.0" encoding="UTF-8"?>
    <plist version="1.0"><dict>
      <key>Label</key><string>net.stepvda.dreamjob.monitoring</string>
      <key>ProgramArguments</key>
      <array>
        <string>/usr/bin/env</string>
        <string>PYTHONPATH=/opt/dreamjob/backend</string>
        <string>/opt/dreamjob/.venv/bin/python</string>
        <string>-m</string><string>dreamjob.monitoring.scheduler</string>
        <string>--once</string>
      </array>
      <key>StartInterval</key><integer>3600</integer>
      <key>StandardOutPath</key><string>/var/log/dreamjob-monitoring.log</string>
      <key>StandardErrorPath</key><string>/var/log/dreamjob-monitoring.log</string>
    </dict></plist>

then ``launchctl load -w`` that file.  The equivalent crontab line is::

    17 * * * * cd /opt/dreamjob && PYTHONPATH=backend .venv/bin/python \\
               -m dreamjob.monitoring.scheduler --once >> /var/log/dreamjob.log 2>&1

``--once`` runs every task whose interval has elapsed and exits, which is why
the same code serves both: the intervals live in the task table, not in the
scheduler that calls them, so cron firing hourly does not turn a daily sweep
into an hourly one.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    BULK,
    close_thread_connections,
    query_all,
    query_one,
    utcnow,
    write_lane,
)
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.monitoring import digest as digest_mod
from dreamjob.monitoring import watchlist as watchlist_mod

log = logging.getLogger(__name__)

SETTING_PREFIX = "scheduler.last_run."
#: How often the loop wakes up to see whether anything is due.
TICK_SECONDS = 60
#: Up to this much random delay before a task runs, so several tasks that come
#: due together do not all hit SQLite in the same instant.
JITTER_SECONDS = 20


@dataclass
class Task:
    """One recurring piece of work and how often it runs."""

    name: str
    interval_seconds: int
    run: Callable[[], Awaitable[dict[str, Any]] | dict[str, Any]]
    description: str = ""
    enabled: bool = True

    def due(self, last_run: str | None, now: datetime) -> bool:
        if not self.enabled:
            return False
        if not last_run:
            return True
        try:
            return datetime.fromisoformat(last_run) + timedelta(
                seconds=self.interval_seconds
            ) <= now
        except ValueError:
            return True


def _last_run(name: str) -> str | None:
    value = kb_repo.get_setting(f"{SETTING_PREFIX}{name}")
    if isinstance(value, dict):
        return value.get("at")
    return value if isinstance(value, str) else None


def _record_run(name: str, result: dict[str, Any] | None, error: str | None = None) -> None:
    kb_repo.set_setting(
        f"{SETTING_PREFIX}{name}",
        {
            "at": utcnow(),
            "error": error,
            "summary": {
                k: v
                for k, v in (result or {}).items()
                if isinstance(v, (int, float, str, bool)) or v is None
            },
        },
    )


# ---------------------------------------------------------------------------
# The tasks
# ---------------------------------------------------------------------------


async def _run_watchlist() -> dict[str, Any]:
    return await watchlist_mod.run_cycle()


async def _run_replies() -> dict[str, Any]:
    from dreamjob.postapp import reply_classifier  # noqa: PLC0415 - avoids an import cycle

    def llm_for(seeker_id: str) -> Any:
        from dreamjob.config import get_settings  # noqa: PLC0415
        from dreamjob.llm.client import LLMClient  # noqa: PLC0415

        if not get_settings().deepseek_api_key:
            return None
        return LLMClient(job_seeker_id=seeker_id)

    return await asyncio.to_thread(
        reply_classifier.process_pending, None, limit=25, llm_factory=llm_for
    )


async def _run_digest() -> dict[str, Any]:
    return await asyncio.to_thread(digest_mod.run_cycle, email=False)


async def _run_follow_ups() -> dict[str, Any]:
    """Notify about follow-ups whose date has passed (FR-327, FR-403).

    Notifies only.  Sending the follow-up is the mail slice's job and needs the
    seeker's approval first (NFR-305).  Runs in a worker thread: it is a
    synchronous loop over every active seeker, and doing it on the serving loop
    stalls all HTTP traffic for the duration.
    """
    return await asyncio.to_thread(_follow_ups_sweep)


def _follow_ups_sweep() -> dict[str, Any]:
    now = utcnow()
    since = (datetime.now(UTC) - timedelta(days=90)).isoformat(timespec="seconds")
    raised = 0
    for seeker_id in repo.active_seeker_ids(since):
        for item in repo.due_follow_ups(seeker_id, now, limit=20):
            if repo.notify(
                seeker_id,
                {
                    "kind": "follow_up_due",
                    "title": (
                        f"Follow-up due: {item.get('company_name') or item.get('recipient_email')}"
                    ),
                    "body": (
                        f"The follow-up for {item.get('opportunity_title') or 'this application'} "
                        f"was due on {str(item.get('follow_up_due_at') or '')[:10]}."
                    ),
                    "payload": {
                        "dispatch_id": item.get("dispatch_id"),
                        "pipeline_card_id": item.get("pipeline_card_id"),
                    },
                    "severity": "action",
                    "dedup_key": f"follow_up:{item.get('dispatch_id')}",
                },
            ):
                raised += 1
    return {"notifications": raised}


async def _run_retention() -> dict[str, Any]:
    """NFR-303: the contacts slice owns the sweep; the scheduler owns the clock."""
    from dreamjob.db.repositories import contacts as contacts_repo  # noqa: PLC0415

    return await asyncio.to_thread(contacts_repo.sweep_retention)


async def _run_llm_redaction() -> dict[str, Any]:
    """FR-364: the administration slice owns the policy; the clock lives here."""
    from dreamjob.api.routers.admin import run_redaction_job  # noqa: PLC0415

    return await asyncio.to_thread(run_redaction_job)


#: How many companies one scheduled enrichment tick covers.  The sweep used to
#: take the 60 busiest in a single pass, and because the passes under it are not
#: actually async (``llm/client.py`` is a synchronous HTTP client, the signals
#: pass is synchronous database work), that pass held the serving loop for
#: 309 s and 385 s on 2026-09-12.  A tick is now bounded, runs on a worker
#: thread, and gives way between companies if a campaign starts.
SWEEP_COMPANIES_PER_TICK = 20

#: Job kinds whose workers own the same registries, egress and single writer
#: the enrichment passes use.  The sweep is background catch-up: it must not
#: start on top of one of these, and it stops between companies if one starts
#: (FR-185, NFR-102).
HEAVY_JOB_KINDS = ("collection", "contacts_discovery")


def _heavy_job_running() -> str | None:
    """The kind of a running collection/discovery job, if there is one."""
    row = query_one(
        "SELECT kind FROM job_run WHERE status = 'running' AND kind IN (?, ?) LIMIT 1",
        HEAVY_JOB_KINDS,
    )
    return str(row["kind"]) if row else None


def _company_enrichment_tick() -> dict[str, Any]:
    """One bounded enrichment sweep, on a worker thread (NFR-102).

    The passes look async but their leaves are not - the website crawl parses
    synchronously, ``company_profile`` synthesises with the synchronous LLM
    client, and ``_refresh_signals`` is plain database work - so this must not
    run on the loop that serves requests.  Deferring while a heavy job runs
    keeps the sweep from competing with the campaign that is already using the
    registries and the single writer.
    """
    from dreamjob.pipeline import company_enrichment  # noqa: PLC0415

    with write_lane(BULK):
        try:
            busy = _heavy_job_running()
            if busy:
                log.info("Company enrichment deferred: a %s job is running", busy)
                return {"deferred": f"{busy} job is running"}

            rows = query_all(
                """
                SELECT o.company_id AS id, COUNT(*) AS n
                FROM opportunity o
                WHERE o.company_id IS NOT NULL
                GROUP BY o.company_id
                ORDER BY n DESC
                LIMIT ?
                """,
                (SWEEP_COMPANIES_PER_TICK,),
            )
            company_ids = [str(r["id"]) for r in rows]
            if not company_ids:
                return {"skipped": "no companies"}

            return asyncio.run(_enrich_companies_off_loop(company_enrichment, company_ids))
        finally:
            # The worker thread outlives the tick; its connection must not
            # (a connection is a file handle onto a database file that may
            # since have been replaced - see jobs/runner.py).
            close_thread_connections()


async def _enrich_companies_off_loop(
    company_enrichment: Any, company_ids: list[str]
) -> dict[str, Any]:
    """Walk the tick's companies, giving way to a heavy job between them."""
    enriched = 0
    employers_resolved = 0
    signals = 0
    for company_id in company_ids:
        busy = _heavy_job_running()
        if busy:
            log.info(
                "Company enrichment yielded after %d company(ies): a %s job started",
                enriched, busy,
            )
            return {"deferred": f"{busy} job started mid-sweep", "companies": enriched}
        # No campaign and no seeker: the sweep runs over the shared knowledge
        # base, which belongs to no one (FR-344), so only the passes that need
        # neither run and the campaign-scoped ones are picked up by the next
        # collection.
        try:
            report = await company_enrichment.enrich_companies(
                [company_id],
                campaign_id=None,
                job_seeker_id=None,
                limit=1,
            )
        except Exception:  # noqa: BLE001 - one company must not stop the tick
            log.exception("Company enrichment failed for %s", company_id)
            continue
        data = report.as_dict()
        enriched += 1
        kinds = data.get("employer_kind") or {}
        if isinstance(kinds, dict):
            employers_resolved += int(kinds.get("resolved") or 0)
        found = data.get("signals") or {}
        if isinstance(found, dict):
            signals += int(found.get("signals") or 0)
    return {
        "companies": enriched,
        "employers_resolved": employers_resolved,
        "signals": signals,
    }


async def _run_company_enrichment() -> dict[str, Any]:
    """Enrich the employers behind the most recent opportunities (FR-221..246).

    The collection tail enriches each campaign's own shortlist, bounded to keep
    the run prompt.  This is the sweep that works through the rest: it takes the
    companies with the most opportunities across every seeker and runs the
    employer-kind ladder, the website crawl, signals and the filings over a
    larger batch, so a knowledge base that has fallen behind catches up on its
    own rather than waiting for someone to press a button.

    The enrichment is awaited here, on the API's own loop, from the scheduler;
    a 60-company pass therefore *was* the 309 s / 385 s stall of 2026-09-12.
    It now runs on a worker thread in the bulk write lane, one bounded tick at
    a time (:data:`SWEEP_COMPANIES_PER_TICK`).
    """
    return await asyncio.to_thread(_company_enrichment_tick)


async def _run_continuous() -> dict[str, Any]:
    """Advance the endless data-collection cycle by one phase (FR-161..166).

    The cycle's own interval is configurable by an administrator, so this task
    ticks every five minutes and the engine decides whether a phase is due.
    The flag is read first (one small setting), then the phase runs; each
    phase defers only on the jobs it would stack on - a running collection
    defers ``discover`` but no longer ``contacts``, ``enrich`` or ``score``
    (FR-185, NFR-102) - and a deferred phase is not stamped, so the next tick
    tries again.
    """
    from dreamjob.pipeline import continuous  # noqa: PLC0415 - avoids an import cycle

    if not continuous.enabled():
        return {"enabled": False}
    return await continuous.run_phase()


def _vacancy_refresh_tick() -> dict[str, Any]:
    """One bounded board refresh, on a worker thread in the bulk write lane.

    The writes under it (vacancy rows, opportunities, scoring) are synchronous
    database work, so like the enrichment sweep it must not run on the loop
    that serves requests.
    """
    from dreamjob.pipeline import vacancy_refresh  # noqa: PLC0415 - avoids an import cycle

    with write_lane(BULK):
        try:
            return asyncio.run(vacancy_refresh.refresh())
        finally:
            close_thread_connections()


async def _run_vacancy_refresh() -> dict[str, Any]:
    """Re-read the ATS boards behind the ranked lists (FR-401, FR-261).

    It is not deferred while a collection runs: one pass is a couple of dozen
    small JSON requests, and deferring it would stop the fast path for as long
    as the continuous cycle keeps a collection going.
    """
    return await asyncio.to_thread(_vacancy_refresh_tick)


def _job_alerts_tick() -> dict[str, Any]:
    """One pass over the connected mailboxes' job-alert e-mails, off the serving loop."""
    from dreamjob.mail import job_alerts  # noqa: PLC0415 - avoids an import cycle

    with write_lane(BULK):
        try:
            return job_alerts.ingest_all()
        finally:
            close_thread_connections()


async def _run_job_alerts() -> dict[str, Any]:
    """Turn job-alert e-mails into ranked opportunities (FR-261).

    Senders are whatever mail/job_alerts.py ALERT_SOURCES declares -
    LinkedIn, ictjob.be, Glassdoor and Jobat today. Jobat rows carry no URL
    and are leads to look up, not links.

    Gmail API calls and the writes under them are synchronous, so the pass runs
    on a worker thread like the other sweeps.
    """
    return await asyncio.to_thread(_job_alerts_tick)


async def _run_learning() -> dict[str, Any]:
    """Tell a seeker when outcome learning has enough evidence to apply (FR-425).

    The effect analysis and the capped weight nudge both exist; what never
    happened was anyone applying them, because nothing said they were ready.
    This only announces readiness - adopting the defaults stays the seeker's
    decision (NFR-305).
    """
    return await asyncio.to_thread(_learning_sweep)


def _learning_sweep() -> dict[str, Any]:
    from dreamjob.postapp import outcomes  # noqa: PLC0415

    seekers = query_all(
        "SELECT DISTINCT job_seeker_id AS id FROM pipeline_card WHERE job_seeker_id IS NOT NULL"
    )
    notified = 0
    for row in seekers:
        seeker_id = str(row["id"])
        try:
            analysis = outcomes.analyse(seeker_id)
        except Exception:  # noqa: BLE001 - one seeker must not stop the sweep
            log.debug("Outcome analysis failed for %s", seeker_id, exc_info=True)
            continue
        if analysis.closed < outcomes.MIN_SAMPLE_TO_REPORT:
            continue
        current = repo.get_learning(seeker_id)
        if current and current.get("applied"):
            continue
        stored = repo.notify(
            seeker_id,
            {
                "kind": "learning_ready",
                "title": "Outcome learning is ready",
                "body": (
                    f"{analysis.closed} of your applications have resolved. "
                    "The results suggest defaults you can review and apply."
                ),
                "payload": {"closed": analysis.closed},
                "dedup_key": f"learning:{analysis.closed}",
            },
        )
        notified += 1 if stored else 0
    return {"seekers": len(seekers), "notified": notified}


HOUR = 3600

#: ``DREAMJOB_VACANCY_REFRESH_SECONDS``; 0 switches the task off.
_VACANCY_REFRESH_SECONDS = get_settings().vacancy_refresh_seconds
#: ``DREAMJOB_JOB_ALERTS_SECONDS``; 0 switches the task off.
_JOB_ALERTS_SECONDS = get_settings().job_alerts_seconds

DEFAULT_TASKS: list[Task] = [
    Task("vacancy_refresh", max(60, _VACANCY_REFRESH_SECONDS), _run_vacancy_refresh,
         "Re-read followed ATS boards and add new postings to the ranked lists",
         enabled=_VACANCY_REFRESH_SECONDS > 0),
    Task("job_alerts", max(60, _JOB_ALERTS_SECONDS), _run_job_alerts,
         "Read LinkedIn, ictjob.be, Glassdoor and Jobat job-alert e-mails into "
         "the ranked lists (Jobat's arrive as leads, with no posting link)",
         enabled=_JOB_ALERTS_SECONDS > 0),
    Task("replies", 15 * 60, _run_replies,
         "Classify incoming replies and draft answers (FR-422)"),
    Task("watchlist", HOUR, _run_watchlist,
         "Recheck watched companies that are due (FR-401, FR-402)"),
    Task("company_enrichment", 6 * HOUR, _run_company_enrichment,
         "Enrich the employers behind the opportunities (FR-221..246, FR-341)"),
    Task("continuous_collection", 300, _run_continuous,
         "Expand the shared data corpus while enabled"),
    Task("follow_ups", HOUR, _run_follow_ups,
         "Notify about follow-ups whose date has passed (FR-327)"),
    Task("learning", 24 * HOUR, _run_learning,
         "Announce outcome learning once it has evidence to apply (FR-425)"),
    Task("digest", 6 * HOUR, _run_digest,
         "Generate the weekly digests that are due (FR-403)"),
    Task("retention", 24 * HOUR, _run_retention,
         "Delete third-party records past their retention date (NFR-303)"),
    Task("llm_redaction", 24 * HOUR, _run_llm_redaction,
         "Redact LLM prompts and responses past their retention (FR-364)"),
]


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


@dataclass
class Scheduler:
    """An asyncio periodic runner.  One instance per process."""

    tasks: list[Task] = field(default_factory=lambda: list(DEFAULT_TASKS))
    tick_seconds: int = TICK_SECONDS
    jitter_seconds: int = JITTER_SECONDS
    _task: asyncio.Task | None = None
    _stop: asyncio.Event | None = None
    #: One lock per task name, so a manual run and the periodic tick cannot run
    #: the same sweep at once (two watchlist passes, double network and writes).
    _locks: dict[str, asyncio.Lock] = field(default_factory=dict)
    started_at: str | None = None
    runs: int = 0

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def task(self, name: str) -> Task | None:
        return next((t for t in self.tasks if t.name == name), None)

    async def run_task(self, task: Task) -> dict[str, Any]:
        """Run one task, recording its outcome either way."""
        lock = self._locks.setdefault(task.name, asyncio.Lock())
        if lock.locked():
            log.info("Scheduler task %s is already running; skipping this trigger", task.name)
            return {"skipped": "already running"}
        async with lock:
            return await self._run_task_locked(task)

    async def _run_task_locked(self, task: Task) -> dict[str, Any]:
        started = datetime.now(UTC)
        try:
            result = task.run()
            if asyncio.iscoroutine(result):
                result = await result
            payload = result if isinstance(result, dict) else {"result": result}
            if payload.get("deferred") and not payload.get("error"):
                # A task that deliberately did not run (a heavy sweep giving way
                # to a running job) must not be stamped as run: the interval
                # would hide it for six hours.  Leaving ``last_run`` alone makes
                # the next tick try again.
                log.info("Scheduler task %s deferred: %s", task.name, payload["deferred"])
                return payload
            _record_run(task.name, payload)
            log.info(
                "Scheduler task %s finished in %.1fs: %s",
                task.name, (datetime.now(UTC) - started).total_seconds(),
                {k: v for k, v in payload.items() if isinstance(v, (int, str, bool))},
            )
            return payload
        except Exception as exc:  # noqa: BLE001 - one task must not stop the loop
            log.exception("Scheduler task %s failed", task.name)
            _record_run(task.name, None, error=str(exc)[:500])
            return {"error": str(exc)[:500]}

    async def run_due(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Run every task whose interval has elapsed.  This is ``--once``."""
        moment = now or datetime.now(UTC)
        outcomes: dict[str, Any] = {}
        for task in self.tasks:
            try:
                if not task.due(_last_run(task.name), moment):
                    continue
                if self.jitter_seconds:
                    await asyncio.sleep(random.uniform(0, self.jitter_seconds))
                outcomes[task.name] = await self.run_task(task)
            except Exception as exc:  # noqa: BLE001 - one task must not stop the tick
                log.exception("Scheduler task %s could not be triggered", task.name)
                outcomes[task.name] = {"error": str(exc)[:500]}
        self.runs += 1
        return outcomes

    async def _loop(self) -> None:
        assert self._stop is not None
        while not self._stop.is_set():
            try:
                await self.run_due()
            except Exception:  # noqa: BLE001 - the loop outlives its iterations
                log.exception("Scheduler tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.tick_seconds)
            except TimeoutError:
                continue

    def start(self) -> dict[str, Any]:
        if self.running:
            return self.status()
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())
        self.started_at = utcnow()
        log.info("Monitoring scheduler started with %d tasks", len(self.tasks))
        return self.status()

    async def stop(self) -> dict[str, Any]:
        if self._stop is not None:
            self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - stopping, not running
                pass
        self._task = None
        self.started_at = None
        log.info("Monitoring scheduler stopped")
        return self.status()

    def status(self) -> dict[str, Any]:
        now = datetime.now(UTC)
        return {
            "running": self.running,
            "started_at": self.started_at,
            "ticks": self.runs,
            "tick_seconds": self.tick_seconds,
            "tasks": [
                {
                    "name": t.name,
                    "description": t.description,
                    "interval_seconds": t.interval_seconds,
                    "enabled": t.enabled,
                    "last_run": kb_repo.get_setting(f"{SETTING_PREFIX}{t.name}"),
                    "due": t.due(_last_run(t.name), now),
                }
                for t in self.tasks
            ],
        }

    def set_enabled(self, name: str, enabled: bool) -> dict[str, Any]:
        task = self.task(name)
        if task is None:
            raise KeyError(f"No scheduler task named {name!r}")
        task.enabled = enabled
        return self.status()


#: The process-wide scheduler.  Started from the API, or by ``--once`` below.
scheduler = Scheduler()


def maybe_autostart() -> dict[str, Any] | None:
    """Start the loop unless the deployment turned it off.

    ``DREAMJOB_SCHEDULER_AUTOSTART`` still wins when set, so an external cron
    setup keeps control; otherwise the ``DREAMJOB_SCHEDULER_ENABLED`` setting
    decides, and it defaults on.  The loop used to be reachable only from the
    admin API, so a running product never checked a watchlist, sent a digest,
    swept retention or redacted a prompt.
    """
    explicit = os.environ.get("DREAMJOB_SCHEDULER_AUTOSTART", "").strip().lower()
    if explicit:
        enabled = explicit in {"1", "true", "yes", "on"}
    else:
        enabled = bool(get_settings().scheduler_enabled)
    if not enabled:
        return None
    return scheduler.start()


async def run_now(name: str) -> dict[str, Any]:
    """Run one task immediately, whether or not it is due (the API's button)."""
    task = scheduler.task(name)
    if task is None:
        raise KeyError(f"No scheduler task named {name!r}")
    return await scheduler.run_task(task)


def main() -> None:  # pragma: no cover - the launchd / cron entry point
    parser = argparse.ArgumentParser(description="Dream Job monitoring scheduler")
    parser.add_argument(
        "--once", action="store_true",
        help="run every task whose interval has elapsed, then exit (for cron or launchd)",
    )
    parser.add_argument("--task", help="run one named task immediately and exit")
    parser.add_argument("--list", action="store_true", help="list the tasks and their intervals")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    from dreamjob.db.migrator import migrate  # noqa: PLC0415 - stand-alone entry point

    migrate()

    if args.list:
        for task in scheduler.tasks:
            print(f"{task.name:<16} every {task.interval_seconds:>6}s  {task.description}")
        return
    if args.task:
        print(asyncio.run(run_now(args.task)))
        return
    if args.once:
        print(asyncio.run(scheduler.run_due()))
        return

    async def forever() -> None:
        scheduler.start()
        assert scheduler._task is not None
        await scheduler._task

    try:
        asyncio.run(forever())
    except KeyboardInterrupt:
        print("stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
