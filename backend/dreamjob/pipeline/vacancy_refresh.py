"""Near-real-time vacancy refresh for the boards people already follow (FR-401, FR-261).

A campaign reads an employer's ATS board once; the continuous cycle re-reads it
at most daily, inside a heavy autopilot run.  A posting that appears an hour
after that read waits a day to reach the ranked list.  This pass closes the gap
cheaply: every few minutes it re-reads a bounded slice of the ATS boards behind
existing opportunities and puts what is new straight into each follower's list.

Why it is built this way:

*Only boards someone follows.*  The boards come from
:func:`pipeline_cards.ranked_ats_boards` - companies that already hold an
opportunity in a live campaign - not from the 15,000-row board registry.  Those
are the employers a seeker has shown interest in, and the set stays small.

*Only ATS boards.*  They are keyless JSON endpoints answered in one request,
and an unchanged board revalidates as a 304 (``egress`` ``max_age``), so a pass
over a quiet board costs a few hundred bytes and no LLM call.

*Round-robin, bounded.*  Each pass reads :data:`BOARDS_PER_PASS` boards after a
cursor kept in ``app_setting``, so a large set is covered over several passes
and a restart resumes where the last pass stopped.

*Same path as the watchlist for what happens next.*  New postings are added by
:func:`watchlist.add_to_ranked_list`, so the campaign's directives filter them
and the new rows are scored exactly as a watched company's would be.  The
notification uses the watchlist's ``vacancy:<id>`` dedup key, so a posting
found by both is announced once.

*"New" means a vacancy row the knowledge-base writer created on this read*
(``WriteOutcome.created``).  It is deliberately not "``collected_at`` after the
read started": the writer re-stamps ``collected_at`` when it merges a posting
it already holds, so a timestamp test would call the whole board new on every
pass and, on a board larger than the query limit, could crowd out the one
posting that really is new.  A merged posting is updated in place and is not
announced again.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any

from dreamjob.adapters.ats import detect as ats_detect
from dreamjob.adapters.base import PlanItem, get_adapter
from dreamjob.db.repositories import admin as admin_repo
from dreamjob.db.repositories import knowledge as kb_repo
from dreamjob.db.repositories import opportunities as opp_repo
from dreamjob.db.repositories import pipeline_cards as repo
from dreamjob.egress.client import EgressClient
from dreamjob.monitoring import watchlist
from dreamjob.pipeline import knowledge_base

log = logging.getLogger(__name__)

#: Boards read per pass.  At the default 10-minute interval this covers 150
#: boards an hour, which is more than a typical installation follows.
BOARDS_PER_PASS = 25

#: Postings read per board, the watchlist's own bound for an ATS recheck.
MAX_RECORDS_PER_BOARD = 200


class BoardUnreadable(RuntimeError):
    """The board could not be read at all (no adapter for its vendor)."""


async def _read_board(board: dict[str, Any], egress: EgressClient) -> list[dict]:
    """Read one ATS board and return the vacancy rows this read created.

    Mirrors :func:`watchlist._ats_board` (same adapter, same plan item, same
    writer) but keeps the writer's outcomes, because only they say which rows
    are new.  An adapter that fetched nothing raises from ``run`` (``settle``),
    and the caller counts it as an error.
    """
    adapter_key = ats_detect.adapter_key_for(board["vendor"])
    if not adapter_key:
        raise BoardUnreadable(f"no adapter for ATS vendor {board['vendor']!r}")
    adapter = get_adapter(adapter_key, egress)
    item = PlanItem(
        adapter_key=adapter_key,
        native_query={
            "slug": board["slug"],
            "company_id": board["company_id"],
            "company_name": board["company_name"],
            "max_records": MAX_RECORDS_PER_BOARD,
        },
        rationale=f"vacancy refresh of {board['company_name']}",
    )
    records = await adapter.run(item)
    writer = knowledge_base.KnowledgeBaseWriter(adapter_key=adapter_key)
    created = [
        outcome.entity_id
        for outcome in writer.write_many(records)
        if outcome.entity_type == "vacancy" and outcome.created
    ]
    return [row for row in (kb_repo.get_vacancy(vid) for vid in created) if row]

#: ``app_setting`` key holding the last company id a pass read.
SETTING_CURSOR = "vacancy_refresh.cursor"


def _group_by_company(rows: list[dict]) -> OrderedDict[str, dict[str, Any]]:
    """One entry per board, carrying every (seeker, campaign) that follows it."""
    boards: OrderedDict[str, dict[str, Any]] = OrderedDict()
    for row in rows:
        board = boards.setdefault(
            str(row["company_id"]),
            {
                "company_id": str(row["company_id"]),
                "company_name": row.get("company_name"),
                "vendor": str(row["ats_vendor"]).lower(),
                "slug": str(row["ats_slug"]),
                "followers": [],
            },
        )
        pair = (str(row["job_seeker_id"]), str(row["campaign_id"]))
        if pair not in board["followers"]:
            board["followers"].append(pair)
    return boards


def next_slice(
    boards: OrderedDict[str, dict[str, Any]], cursor: str | None, size: int
) -> list[dict[str, Any]]:
    """The ``size`` boards after ``cursor``, wrapping round to the start."""
    keys = list(boards)
    if not keys:
        return []
    start = 0
    if cursor in boards:
        start = keys.index(cursor) + 1
    else:
        # The cursor company left the set (its campaign was cancelled): resume
        # at the next id in order rather than restarting from the first board.
        start = next((i for i, key in enumerate(keys) if cursor and key > cursor), 0)
    ordered = keys[start:] + keys[:start]
    return [boards[key] for key in ordered[: max(0, size)]]


def notify_added(seeker_id: str, board: dict[str, Any], opportunity_ids: list[str]) -> int:
    """One ``new_vacancy`` notification per opportunity this pass added."""
    sent = 0
    for opportunity_id in opportunity_ids:
        opportunity = opp_repo.get_opportunity(opportunity_id, seeker_id) or {}
        vacancy_id = opportunity.get("vacancy_id")
        if not vacancy_id:
            continue
        stored = repo.notify(
            seeker_id,
            {
                "kind": "new_vacancy",
                "title": f"{board.get('company_name') or 'A followed company'}: "
                         f"{opportunity.get('title') or 'new role'}",
                "body": opportunity.get("location") or "",
                "payload": {
                    "company_id": board["company_id"],
                    "vacancy_id": vacancy_id,
                    "opportunity_id": opportunity_id,
                    "source_url": opportunity.get("source_url"),
                    "added_to_ranked_list": True,
                },
                "severity": "action",
                "dedup_key": f"vacancy:{vacancy_id}",
            },
        )
        sent += 1 if stored else 0
    return sent


async def refresh(*, limit: int = BOARDS_PER_PASS) -> dict[str, Any]:
    """Re-read the next slice of followed ATS boards and feed what is new.

    Returns counters for ``scheduler.last_run``.  One failing board is counted
    in ``errors`` and never stops the pass.
    """
    boards = _group_by_company(repo.ranked_ats_boards())
    chosen = next_slice(boards, admin_repo.get_setting(SETTING_CURSOR), limit)
    if not chosen:
        return {"skipped": "no followed ATS boards"}

    report = {"boards": 0, "new_vacancies": 0, "opportunities_added": 0,
              "notifications": 0, "errors": 0, "followed_boards": len(boards)}
    async with EgressClient() as egress:
        for board in chosen:
            try:
                new_rows = await _read_board(board, egress)
            except Exception as exc:  # noqa: BLE001 - a dead board is counted, not raised
                log.info("Vacancy refresh: %s/%s not read: %s",
                         board["vendor"], board["slug"], f"{type(exc).__name__}: {exc}"[:300])
                new_rows = None
            admin_repo.set_setting(SETTING_CURSOR, board["company_id"])
            report["boards"] += 1
            if new_rows is None:
                report["errors"] += 1
                continue
            if not new_rows:
                continue
            report["new_vacancies"] += len(new_rows)
            for seeker_id, campaign_id in board["followers"]:
                try:
                    added = watchlist.add_to_ranked_list(seeker_id, campaign_id, new_rows)
                except Exception:  # noqa: BLE001 - one campaign must not stop the board
                    log.exception("Vacancy refresh could not add to campaign %s", campaign_id)
                    report["errors"] += 1
                    continue
                report["opportunities_added"] += len(added)
                report["notifications"] += notify_added(seeker_id, board, added)
    return report
