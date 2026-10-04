"""Post-application pipeline and monitoring SQL (FR-401..403, FR-421..425, FR-444).

Every table reached from here is **private**: pipeline cards, reply drafts,
interview appointments, calendar grants, mock-interview sessions, negotiation
briefs, the outcome-learning record, the watchlist, notifications and digests.
So every statement takes a ``job_seeker_id`` and filters on it (FR-101,
FR-344), including the reads that look like lookups by primary key - an id is
not an authorisation.

The shared rows these cards point at - ``company``, ``vacancy``,
``hiring_signal`` - are only ever joined *outwards* from a private row the
caller has already proved they own.

Two areas live in one module because they are one area in the data: the
monitoring loop (FR-401..403) exists to feed the board (FR-421..425), and the
digest reads six of these tables in a single pass.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from dreamjob.config import get_settings
from dreamjob.db.connection import (
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    utcnow,
    write_tx,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

CARD_JSON = ("stage_dates", "variables", "outcome_detail")
APPOINTMENT_JSON = ("proposed_slots", "availability", "recommended")
SESSION_JSON = ("transcript", "feedback", "weak_spots", "question_plan")
BRIEF_JSON = ("arguments", "fallbacks", "market_data", "inputs")
LEARNING_JSON = ("effects", "defaults", "weight_adjustment")
WATCH_JSON = ("check_sources", "last_result")


def _decode(row: dict | None, json_columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for column in json_columns:
        if column in out:
            out[column] = from_json(out[column], None)
    return out


def _decode_all(rows: list[dict], json_columns: tuple[str, ...]) -> list[dict]:
    return [_decode(r, json_columns) for r in rows]  # type: ignore[misc]


# ---------------------------------------------------------------------------
# FR-421: pipeline cards
# ---------------------------------------------------------------------------

_CARD_SELECT = """
    SELECT k.*,
           o.title             AS opportunity_title,
           o.kind              AS opportunity_kind,
           o.score             AS opportunity_score,
           o.language          AS opportunity_language,
           o.company_id        AS company_id,
           c.name              AS company_name,
           c.domain            AS company_domain,
           p.language          AS package_language,
           p.cv_template       AS cv_template,
           p.briefing_pdf_path AS briefing_pdf_path,
           p.motivation_pdf_path AS motivation_pdf_path,
           p.contact_id        AS contact_id,
           d.id                AS dispatch_id,
           d.sent_at           AS sent_at,
           d.thread_id         AS thread_id,
           d.recipient_email   AS recipient_email,
           d.follow_up_due_at  AS follow_up_due_at,
           d.follow_up_sent_at AS follow_up_sent_at,
           d.delivery_status   AS delivery_status
    FROM pipeline_card k
    LEFT JOIN opportunity o ON o.id = k.opportunity_id
    LEFT JOIN company c ON c.id = o.company_id
    LEFT JOIN application_package p ON p.id = k.application_package_id
    LEFT JOIN dispatch d ON d.id = (
        SELECT id FROM dispatch
        WHERE application_package_id = k.application_package_id
          AND COALESCE(kind, 'application') = 'application'
        ORDER BY COALESCE(sent_at, created_at) ASC LIMIT 1
    )
"""


def get_card(card_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            _CARD_SELECT + " WHERE k.id = ? AND k.job_seeker_id = ?", (card_id, job_seeker_id)
        ),
        CARD_JSON,
    )


def card_for_package(job_seeker_id: str, application_package_id: str) -> dict | None:
    return _decode(
        query_one(
            _CARD_SELECT + " WHERE k.job_seeker_id = ? AND k.application_package_id = ?",
            (job_seeker_id, application_package_id),
        ),
        CARD_JSON,
    )


def card_for_opportunity(job_seeker_id: str, opportunity_id: str) -> dict | None:
    return _decode(
        query_one(
            _CARD_SELECT + " WHERE k.job_seeker_id = ? AND k.opportunity_id = ? "
            "ORDER BY k.created_at DESC LIMIT 1",
            (job_seeker_id, opportunity_id),
        ),
        CARD_JSON,
    )


def card_for_dispatch(job_seeker_id: str, dispatch_id: str) -> dict | None:
    """The card an incoming reply belongs to, reached through its dispatch."""
    return _decode(
        query_one(
            _CARD_SELECT
            + """ WHERE k.job_seeker_id = ? AND k.application_package_id = (
                    SELECT application_package_id FROM dispatch
                    WHERE id = ? AND job_seeker_id = ?
                  )""",
            (job_seeker_id, dispatch_id, job_seeker_id),
        ),
        CARD_JSON,
    )


def list_cards(
    job_seeker_id: str,
    *,
    stage: str | None = None,
    campaign_id: str | None = None,
    include_closed: bool = True,
    limit: int = 500,
) -> list[dict]:
    sql = _CARD_SELECT + " WHERE k.job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if stage:
        sql += " AND k.stage = ?"
        params.append(stage)
    if campaign_id:
        sql += " AND o.campaign_id = ?"
        params.append(campaign_id)
    if not include_closed:
        sql += " AND k.stage <> 'closed'"
    sql += " ORDER BY COALESCE(k.next_action_due, k.updated_at) ASC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), CARD_JSON)


def create_card(job_seeker_id: str, values: dict) -> str:
    now = utcnow()
    payload = {
        "job_seeker_id": job_seeker_id,
        "stage": "sent",
        "created_at": now,
        "updated_at": now,
        "stage_changed_at": now,
        **values,
    }
    card_id = insert_row("pipeline_card", payload)
    invalidate_active_seeker_cache()
    return card_id


def update_card(card_id: str, values: dict) -> None:
    if not values:
        return
    update_row("pipeline_card", card_id, {**values, "updated_at": utcnow()})
    invalidate_active_seeker_cache()


def delete_card(card_id: str, job_seeker_id: str) -> int:
    with write_tx() as conn:
        deleted = conn.execute(
            "DELETE FROM pipeline_card WHERE id = ? AND job_seeker_id = ?",
            (card_id, job_seeker_id),
        ).rowcount
    if deleted:
        invalidate_active_seeker_cache()
    return deleted


def stage_counts(job_seeker_id: str) -> dict[str, int]:
    rows = query_all(
        "SELECT stage, COUNT(*) AS n FROM pipeline_card WHERE job_seeker_id = ? GROUP BY stage",
        (job_seeker_id,),
    )
    return {r["stage"]: int(r["n"]) for r in rows}


def cards_due(job_seeker_id: str, before: str, limit: int = 100) -> list[dict]:
    """Cards whose next action is due - the follow-up reminder feed (FR-403)."""
    return _decode_all(
        query_all(
            _CARD_SELECT + " WHERE k.job_seeker_id = ? AND k.next_action_due IS NOT NULL "
            "AND k.next_action_due <= ? AND k.stage <> 'closed' "
            "ORDER BY k.next_action_due ASC LIMIT ?",
            (job_seeker_id, before, int(limit)),
        ),
        CARD_JSON,
    )


def silent_cards(job_seeker_id: str, since: str, limit: int = 200) -> list[dict]:
    """Sent, never answered, and older than ``since`` (FR-425 'no response')."""
    return _decode_all(
        query_all(
            _CARD_SELECT + " WHERE k.job_seeker_id = ? AND k.stage = 'sent' "
            "AND k.reached_replied_at IS NULL AND k.created_at <= ? "
            "ORDER BY k.created_at ASC LIMIT ?",
            (job_seeker_id, since, int(limit)),
        ),
        CARD_JSON,
    )


def sent_packages_without_card(job_seeker_id: str, limit: int = 500) -> list[dict]:
    """Sent applications the board does not know about yet (FR-421)."""
    return query_all(
        """
        SELECT p.id AS application_package_id, p.opportunity_id, p.language, p.cv_template,
               p.contact_id, p.email_subject, p.created_at AS package_created_at,
               d.id AS dispatch_id, d.sent_at, d.thread_id, d.recipient_email,
               d.follow_up_due_at, d.backend
        FROM application_package p
        JOIN dispatch d ON d.application_package_id = p.id
                       AND COALESCE(d.kind, 'application') = 'application'
        LEFT JOIN pipeline_card k ON k.application_package_id = p.id
        WHERE p.job_seeker_id = ? AND k.id IS NULL
          AND d.delivery_status IN ('sent', 'delivered')
        ORDER BY d.sent_at ASC LIMIT ?
        """,
        (job_seeker_id, int(limit)),
    )


# --- FR-421: the transition log --------------------------------------------


def record_card_event(job_seeker_id: str, card_id: str, values: dict) -> str:
    return insert_row(
        "pipeline_card_event",
        {
            "job_seeker_id": job_seeker_id,
            "pipeline_card_id": card_id,
            "created_at": utcnow(),
            **values,
        },
    )


def card_events(card_id: str, job_seeker_id: str, limit: int = 100) -> list[dict]:
    rows = query_all(
        "SELECT * FROM pipeline_card_event WHERE pipeline_card_id = ? AND job_seeker_id = ? "
        "ORDER BY created_at ASC LIMIT ?",
        (card_id, job_seeker_id, int(limit)),
    )
    for row in rows:
        row["detail"] = from_json(row["detail"], None)
    return rows


# ---------------------------------------------------------------------------
# FR-422: incoming replies and their drafts
# ---------------------------------------------------------------------------


def get_reply(reply_id: str, job_seeker_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM incoming_reply WHERE id = ? AND job_seeker_id = ?",
        (reply_id, job_seeker_id),
    )
    return _decode(row, ("extracted_slots",))


def list_replies(
    job_seeker_id: str, *, handled: bool | None = None, since: str | None = None, limit: int = 200
) -> list[dict]:
    sql = """
        SELECT r.*, d.application_package_id, o.title AS opportunity_title,
               c.name AS company_name
        FROM incoming_reply r
        LEFT JOIN dispatch d ON d.id = r.dispatch_id
        LEFT JOIN application_package p ON p.id = d.application_package_id
        LEFT JOIN opportunity o ON o.id = p.opportunity_id
        LEFT JOIN company c ON c.id = o.company_id
        WHERE r.job_seeker_id = ?
    """
    params: list[Any] = [job_seeker_id]
    if handled is not None:
        sql += " AND r.handled = ?"
        params.append(1 if handled else 0)
    if since:
        sql += " AND COALESCE(r.received_at, r.created_at) >= ?"
        params.append(since)
    sql += " ORDER BY COALESCE(r.received_at, r.created_at) DESC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), ("extracted_slots",))


def unclassified_replies(job_seeker_id: str | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM incoming_reply WHERE classification IS NULL"
    params: list[Any] = []
    if job_seeker_id:
        sql += " AND job_seeker_id = ?"
        params.append(job_seeker_id)
    sql += " ORDER BY COALESCE(received_at, created_at) ASC LIMIT ?"
    params.append(int(limit))
    return query_all(sql, tuple(params))


def save_classification(reply_id: str, values: dict) -> None:
    update_row("incoming_reply", reply_id, values)


def mark_reply_handled(reply_id: str, job_seeker_id: str, handled: bool = True) -> int:
    with write_tx() as conn:
        return conn.execute(
            "UPDATE incoming_reply SET handled = ? WHERE id = ? AND job_seeker_id = ?",
            (1 if handled else 0, reply_id, job_seeker_id),
        ).rowcount


def create_draft(job_seeker_id: str, values: dict) -> str:
    now = utcnow()
    return insert_row(
        "reply_draft",
        {"job_seeker_id": job_seeker_id, "created_at": now, "updated_at": now, **values},
    )


def update_draft(draft_id: str, values: dict) -> None:
    if not values:
        return
    update_row("reply_draft", draft_id, {**values, "updated_at": utcnow()})


def get_draft(draft_id: str, job_seeker_id: str) -> dict | None:
    row = query_one(
        "SELECT * FROM reply_draft WHERE id = ? AND job_seeker_id = ?", (draft_id, job_seeker_id)
    )
    return _decode(row, ("attachments",))


def drafts_for_reply(incoming_reply_id: str, job_seeker_id: str) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT * FROM reply_draft WHERE incoming_reply_id = ? AND job_seeker_id = ? "
            "ORDER BY created_at DESC",
            (incoming_reply_id, job_seeker_id),
        ),
        ("attachments",),
    )


def list_drafts(job_seeker_id: str, *, status: str | None = None, limit: int = 200) -> list[dict]:
    sql = "SELECT * FROM reply_draft WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), ("attachments",))


# ---------------------------------------------------------------------------
# FR-423: calendar grants and interview appointments
# ---------------------------------------------------------------------------


def get_calendar_account(job_seeker_id: str, provider: str | None = None) -> dict | None:
    sql = "SELECT * FROM calendar_account WHERE job_seeker_id = ? AND is_active = 1"
    params: list[Any] = [job_seeker_id]
    if provider:
        sql += " AND provider = ?"
        params.append(provider)
    sql += " ORDER BY connected_at DESC LIMIT 1"
    return query_one(sql, tuple(params))


def list_calendar_accounts(job_seeker_id: str) -> list[dict]:
    """Never returns ``credentials_enc``: the API must not be able to leak it."""
    return query_all(
        "SELECT id, provider, address, calendar_id, timezone, scopes, token_expires_at, "
        "is_active, last_error, connected_at FROM calendar_account WHERE job_seeker_id = ? "
        "ORDER BY connected_at DESC",
        (job_seeker_id,),
    )


def save_calendar_account(job_seeker_id: str, values: dict) -> str:
    existing = query_one(
        "SELECT id FROM calendar_account WHERE job_seeker_id = ? AND provider = ? AND address = ?",
        (job_seeker_id, values.get("provider"), values.get("address")),
    )
    if existing:
        update_row("calendar_account", existing["id"], values)
        return str(existing["id"])
    return insert_row(
        "calendar_account",
        {"job_seeker_id": job_seeker_id, "connected_at": utcnow(), **values},
    )


def update_calendar_account(account_id: str, values: dict) -> None:
    update_row("calendar_account", account_id, values)


def delete_calendar_account(account_id: str, job_seeker_id: str) -> int:
    """NFR-204: revoking the grant means the row and its token are gone."""
    with write_tx() as conn:
        return conn.execute(
            "DELETE FROM calendar_account WHERE id = ? AND job_seeker_id = ?",
            (account_id, job_seeker_id),
        ).rowcount


def create_appointment(job_seeker_id: str, values: dict) -> str:
    now = utcnow()
    return insert_row(
        "interview_appointment",
        {"job_seeker_id": job_seeker_id, "created_at": now, "updated_at": now, **values},
    )


def update_appointment(appointment_id: str, values: dict) -> None:
    if not values:
        return
    update_row("interview_appointment", appointment_id, {**values, "updated_at": utcnow()})


def get_appointment(appointment_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM interview_appointment WHERE id = ? AND job_seeker_id = ?",
            (appointment_id, job_seeker_id),
        ),
        APPOINTMENT_JSON,
    )


def appointment_for_reply(incoming_reply_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM interview_appointment WHERE incoming_reply_id = ? "
            "AND job_seeker_id = ? ORDER BY created_at DESC LIMIT 1",
            (incoming_reply_id, job_seeker_id),
        ),
        APPOINTMENT_JSON,
    )


def list_appointments(
    job_seeker_id: str, *, status: str | None = None, limit: int = 100
) -> list[dict]:
    sql = "SELECT * FROM interview_appointment WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY COALESCE(chosen_start, created_at) DESC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), APPOINTMENT_JSON)


# ---------------------------------------------------------------------------
# FR-424: mock interview sessions
# ---------------------------------------------------------------------------


def create_session(job_seeker_id: str, values: dict) -> str:
    now = utcnow()
    return insert_row(
        "mock_interview_session",
        {"job_seeker_id": job_seeker_id, "created_at": now, "updated_at": now, **values},
    )


def update_session(session_id: str, values: dict) -> None:
    if not values:
        return
    update_row("mock_interview_session", session_id, {**values, "updated_at": utcnow()})


def get_session(session_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT s.*, o.title AS opportunity_title, c.name AS company_name "
            "FROM mock_interview_session s "
            "LEFT JOIN opportunity o ON o.id = s.opportunity_id "
            "LEFT JOIN company c ON c.id = o.company_id "
            "WHERE s.id = ? AND s.job_seeker_id = ?",
            (session_id, job_seeker_id),
        ),
        SESSION_JSON,
    )


def list_sessions(
    job_seeker_id: str, *, opportunity_id: str | None = None, limit: int = 50
) -> list[dict]:
    sql = (
        "SELECT s.id, s.opportunity_id, s.status, s.round_number, s.language, s.focus, "
        "s.summary, s.weak_spots, s.created_at, s.updated_at, s.finished_at, "
        "o.title AS opportunity_title, c.name AS company_name "
        "FROM mock_interview_session s "
        "LEFT JOIN opportunity o ON o.id = s.opportunity_id "
        "LEFT JOIN company c ON c.id = o.company_id "
        "WHERE s.job_seeker_id = ?"
    )
    params: list[Any] = [job_seeker_id]
    if opportunity_id:
        sql += " AND s.opportunity_id = ?"
        params.append(opportunity_id)
    sql += " ORDER BY s.created_at DESC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), ("weak_spots",))


def next_round_number(job_seeker_id: str, opportunity_id: str) -> int:
    row = query_one(
        "SELECT MAX(round_number) AS n FROM mock_interview_session "
        "WHERE job_seeker_id = ? AND opportunity_id = ?",
        (job_seeker_id, opportunity_id),
    )
    return int((row or {}).get("n") or 0) + 1


def weak_spot_history(job_seeker_id: str, opportunity_id: str, limit: int = 10) -> list[dict]:
    """Earlier rounds, so a repeat session can open on what was weak (FR-424)."""
    rows = query_all(
        "SELECT round_number, weak_spots, finished_at FROM mock_interview_session "
        "WHERE job_seeker_id = ? AND opportunity_id = ? AND weak_spots IS NOT NULL "
        "ORDER BY round_number DESC LIMIT ?",
        (job_seeker_id, opportunity_id, int(limit)),
    )
    return _decode_all(rows, ("weak_spots",))


# ---------------------------------------------------------------------------
# FR-444: negotiation briefs
# ---------------------------------------------------------------------------


def save_brief(job_seeker_id: str, opportunity_id: str, values: dict) -> str:
    now = utcnow()
    existing = query_one(
        "SELECT id FROM negotiation_brief WHERE job_seeker_id = ? AND opportunity_id = ?",
        (job_seeker_id, opportunity_id),
    )
    if existing:
        update_row("negotiation_brief", existing["id"], {**values, "updated_at": now})
        return str(existing["id"])
    return insert_row(
        "negotiation_brief",
        {
            "job_seeker_id": job_seeker_id,
            "opportunity_id": opportunity_id,
            "created_at": now,
            "updated_at": now,
            **values,
        },
    )


def get_brief(job_seeker_id: str, opportunity_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM negotiation_brief WHERE job_seeker_id = ? AND opportunity_id = ?",
            (job_seeker_id, opportunity_id),
        ),
        BRIEF_JSON,
    )


def list_briefs(job_seeker_id: str, limit: int = 100) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT b.*, o.title AS opportunity_title, c.name AS company_name "
            "FROM negotiation_brief b "
            "LEFT JOIN opportunity o ON o.id = b.opportunity_id "
            "LEFT JOIN company c ON c.id = o.company_id "
            "WHERE b.job_seeker_id = ? ORDER BY b.created_at DESC LIMIT ?",
            (job_seeker_id, int(limit)),
        ),
        BRIEF_JSON,
    )


# ---------------------------------------------------------------------------
# FR-425: outcome learning
# ---------------------------------------------------------------------------


def outcome_rows(job_seeker_id: str, limit: int = 1000) -> list[dict]:
    """One row per sent application with its variables and its outcome.

    The variables were frozen onto the card at send time (FR-425), so this
    reads what was true *then*, not what the package looks like now.
    """
    rows = query_all(
        """
        SELECT k.id, k.stage, k.outcome, k.variables, k.created_at, k.updated_at,
               k.reached_replied_at, k.reached_interview_at, k.reached_offer_at,
               o.kind AS opportunity_kind, o.score, o.function_family, o.seniority,
               o.country, o.work_arrangement, c.size_band, c.stage AS company_stage
        FROM pipeline_card k
        LEFT JOIN opportunity o ON o.id = k.opportunity_id
        LEFT JOIN company c ON c.id = o.company_id
        WHERE k.job_seeker_id = ?
        ORDER BY k.created_at DESC LIMIT ?
        """,
        (job_seeker_id, int(limit)),
    )
    return _decode_all(rows, ("variables",))


def send_variables_source(job_seeker_id: str, application_package_id: str) -> dict | None:
    """The package, its send and its contact as they stood at send time (FR-425).

    One read, taken once, because the package can be regenerated and the
    retention sweep (NFR-303) may delete the contact long before the outcome
    is known.
    """
    return query_one(
        """
        SELECT p.language, p.cv_template, p.email_body, p.contact_id, p.opportunity_id,
               d.sent_at, d.backend, d.recipient_email,
               o.kind AS opportunity_kind, o.score, o.company_id,
               c.is_generic_mailbox, c.department, c.role_title, c.email_validation
        FROM application_package p
        LEFT JOIN dispatch d ON d.application_package_id = p.id
                             AND COALESCE(d.kind, 'application') = 'application'
        LEFT JOIN opportunity o ON o.id = p.opportunity_id
        LEFT JOIN contact c ON c.id = p.contact_id
        WHERE p.id = ? AND p.job_seeker_id = ?
        ORDER BY d.sent_at ASC LIMIT 1
        """,
        (application_package_id, job_seeker_id),
    )


def has_introduction_path(job_seeker_id: str, opportunity_id: str | None) -> bool:
    """Whether a warm introduction was in play for this opportunity (FR-425)."""
    if not opportunity_id:
        return False
    row = query_one(
        "SELECT COUNT(*) AS n FROM introduction_path WHERE job_seeker_id = ? "
        "AND opportunity_id = ? AND status <> 'rejected'",
        (job_seeker_id, opportunity_id),
    )
    return int((row or {}).get("n") or 0) > 0


def get_learning(job_seeker_id: str) -> dict | None:
    return _decode(
        query_one("SELECT * FROM outcome_learning WHERE job_seeker_id = ?", (job_seeker_id,)),
        LEARNING_JSON,
    )


def save_learning(job_seeker_id: str, values: dict) -> str:
    existing = query_one(
        "SELECT id FROM outcome_learning WHERE job_seeker_id = ?", (job_seeker_id,)
    )
    payload = {"computed_at": utcnow(), **values}
    if existing:
        update_row("outcome_learning", existing["id"], payload)
        return str(existing["id"])
    return insert_row("outcome_learning", {"job_seeker_id": job_seeker_id, **payload})


# ---------------------------------------------------------------------------
# FR-401: the watchlist
# ---------------------------------------------------------------------------


_WATCH_SELECT = """
    SELECT w.*, c.name AS company_name, c.domain AS company_domain,
           c.careers_url AS careers_url, c.ats_vendor AS ats_vendor,
           c.ats_slug AS ats_slug, c.country AS company_country,
           c.source AS company_source
    FROM watchlist_entry w JOIN company c ON c.id = w.company_id
"""


def get_watch(entry_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            _WATCH_SELECT + " WHERE w.id = ? AND w.job_seeker_id = ?", (entry_id, job_seeker_id)
        ),
        WATCH_JSON,
    )


def find_watch(job_seeker_id: str, company_id: str) -> dict | None:
    return _decode(
        query_one(
            _WATCH_SELECT + " WHERE w.job_seeker_id = ? AND w.company_id = ?",
            (job_seeker_id, company_id),
        ),
        WATCH_JSON,
    )


def list_watches(job_seeker_id: str, *, active_only: bool = False) -> list[dict]:
    sql = _WATCH_SELECT + " WHERE w.job_seeker_id = ?"
    if active_only:
        sql += " AND w.active = 1"
    sql += " ORDER BY c.name COLLATE NOCASE ASC"
    return _decode_all(query_all(sql, (job_seeker_id,)), WATCH_JSON)


def upsert_watch(job_seeker_id: str, company_id: str, values: dict) -> tuple[str, bool]:
    existing = query_one(
        "SELECT id FROM watchlist_entry WHERE job_seeker_id = ? AND company_id = ?",
        (job_seeker_id, company_id),
    )
    if existing:
        update_row("watchlist_entry", existing["id"], values)
        invalidate_active_seeker_cache()
        return str(existing["id"]), False
    entry_id = insert_row(
        "watchlist_entry",
        {
            "job_seeker_id": job_seeker_id,
            "company_id": company_id,
            "active": 1,
            "created_at": utcnow(),
            **values,
        },
    )
    invalidate_active_seeker_cache()
    return entry_id, True


def update_watch(entry_id: str, values: dict) -> None:
    update_row("watchlist_entry", entry_id, values)
    invalidate_active_seeker_cache()


def delete_watch(entry_id: str, job_seeker_id: str) -> int:
    with write_tx() as conn:
        deleted = conn.execute(
            "DELETE FROM watchlist_entry WHERE id = ? AND job_seeker_id = ?",
            (entry_id, job_seeker_id),
        ).rowcount
    if deleted:
        invalidate_active_seeker_cache()
    return deleted


def watches_due(now: str, *, job_seeker_id: str | None = None, limit: int = 200) -> list[dict]:
    """Active watches whose interval has elapsed (FR-401).

    A watch that has never run has no ``next_check_at``, so it is due at once;
    that is what makes adding a company to the watchlist act immediately.
    """
    sql = _WATCH_SELECT + (
        " WHERE w.active = 1 AND (w.next_check_at IS NULL OR w.next_check_at <= ?)"
    )
    params: list[Any] = [now]
    if job_seeker_id:
        sql += " AND w.job_seeker_id = ?"
        params.append(job_seeker_id)
    sql += " ORDER BY COALESCE(w.next_check_at, w.created_at) ASC LIMIT ?"
    params.append(int(limit))
    return _decode_all(query_all(sql, tuple(params)), WATCH_JSON)


def vacancies_since(company_id: str, since: str | None, limit: int = 100) -> list[dict]:
    """Vacancies of a watched company first seen after the previous check."""
    sql = "SELECT * FROM vacancy WHERE company_id = ?"
    params: list[Any] = [company_id]
    if since:
        sql += " AND collected_at > ?"
        params.append(since)
    sql += " ORDER BY collected_at DESC LIMIT ?"
    params.append(int(limit))
    return query_all(sql, tuple(params))


def ranked_ats_boards() -> list[dict]:
    """Every ATS board behind someone's ranked list, with who follows it.

    One row per ``(company, seeker, campaign)``: the vacancy refresh reads each
    board once and hands its new postings to every campaign that already holds
    an opportunity at that company.  Like :func:`watches_due` this crosses
    seekers on purpose - it is the scheduler asking "what is followed?" - and
    it returns only what the refresh needs, never another seeker's row content.
    A cancelled campaign is left out: nobody is reading its list.
    """
    return query_all(
        "SELECT DISTINCT o.company_id, o.job_seeker_id, o.campaign_id, "
        "       c.name AS company_name, c.ats_vendor, c.ats_slug "
        "FROM opportunity o "
        "JOIN company c ON c.id = o.company_id "
        "JOIN campaign k ON k.id = o.campaign_id "
        "WHERE c.ats_vendor IS NOT NULL AND c.ats_vendor <> '' "
        "  AND c.ats_slug IS NOT NULL AND c.ats_slug <> '' "
        "  AND k.status <> 'cancelled' "
        "ORDER BY o.company_id"
    )


def signals_since(company_id: str, since: str | None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM hiring_signal WHERE company_id = ?"
    params: list[Any] = [company_id]
    if since:
        sql += " AND collected_at > ?"
        params.append(since)
    sql += " ORDER BY collected_at DESC LIMIT ?"
    params.append(int(limit))
    return query_all(sql, tuple(params))


def filings_since(company_id: str, since: str | None, limit: int = 20) -> list[dict]:
    sql = (
        "SELECT id, fiscal_year, period_end, revenue, headcount_fte, source, collected_at, "
        "is_estimated FROM financial_year WHERE company_id = ?"
    )
    params: list[Any] = [company_id]
    if since:
        sql += " AND collected_at > ?"
        params.append(since)
    sql += " ORDER BY fiscal_year DESC LIMIT ?"
    params.append(int(limit))
    return query_all(sql, tuple(params))


# ---------------------------------------------------------------------------
# FR-401/FR-403: notifications
# ---------------------------------------------------------------------------


def notify(job_seeker_id: str, values: dict) -> str | None:
    """Insert one notification.  A repeated ``dedup_key`` is silently dropped.

    FR-401 asks for a notification when something new appears, which means the
    same vacancy must not be announced by every weekly pass.  The unique index
    does the de-duplication; catching its violation here keeps every caller
    free of that concern.
    """
    payload = {"job_seeker_id": job_seeker_id, "created_at": utcnow(), **values}
    if isinstance(payload.get("payload"), (dict, list)):
        payload["payload"] = to_json(payload["payload"])
    try:
        return insert_row("notification", payload)
    except Exception as exc:  # noqa: BLE001 - a duplicate announcement is not an error
        log.debug("Notification not stored (%s): %s", payload.get("dedup_key"), exc)
        return None


def list_notifications(
    job_seeker_id: str,
    *,
    unread_only: bool = False,
    kind: str | None = None,
    since: str | None = None,
    limit: int = 100,
) -> list[dict]:
    sql = "SELECT * FROM notification WHERE job_seeker_id = ? AND dismissed_at IS NULL"
    params: list[Any] = [job_seeker_id]
    if unread_only:
        sql += " AND read_at IS NULL"
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if since:
        sql += " AND created_at >= ?"
        params.append(since)
    sql += " ORDER BY created_at DESC LIMIT ?"
    params.append(int(limit))
    rows = query_all(sql, tuple(params))
    for row in rows:
        row["payload"] = from_json(row["payload"], None)
    return rows


def unread_count(job_seeker_id: str) -> int:
    row = query_one(
        "SELECT COUNT(*) AS n FROM notification WHERE job_seeker_id = ? AND read_at IS NULL "
        "AND dismissed_at IS NULL",
        (job_seeker_id,),
    )
    return int((row or {}).get("n") or 0)


def mark_notification(notification_id: str, job_seeker_id: str, field: str = "read_at") -> int:
    if field not in ("read_at", "dismissed_at"):
        raise ValueError("field must be read_at or dismissed_at")
    with write_tx() as conn:
        return conn.execute(
            f"UPDATE notification SET {field} = ? WHERE id = ? AND job_seeker_id = ?",
            (utcnow(), notification_id, job_seeker_id),
        ).rowcount


def mark_all_read(job_seeker_id: str) -> int:
    with write_tx() as conn:
        return conn.execute(
            "UPDATE notification SET read_at = ? WHERE job_seeker_id = ? AND read_at IS NULL",
            (utcnow(), job_seeker_id),
        ).rowcount


# ---------------------------------------------------------------------------
# FR-403: digests
# ---------------------------------------------------------------------------


def save_digest(job_seeker_id: str, values: dict) -> str:
    existing = query_one(
        "SELECT id FROM digest WHERE job_seeker_id = ? AND period_start = ? AND period_end = ?",
        (job_seeker_id, values.get("period_start"), values.get("period_end")),
    )
    if existing:
        update_row("digest", existing["id"], values)
        return str(existing["id"])
    return insert_row(
        "digest", {"job_seeker_id": job_seeker_id, "created_at": utcnow(), **values}
    )


def get_digest(digest_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM digest WHERE id = ? AND job_seeker_id = ?", (digest_id, job_seeker_id)
        ),
        ("content", "recommended_action_detail"),
    )


def latest_digest(job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM digest WHERE job_seeker_id = ? ORDER BY period_end DESC LIMIT 1",
            (job_seeker_id,),
        ),
        ("content", "recommended_action_detail"),
    )


def list_digests(job_seeker_id: str, limit: int = 20) -> list[dict]:
    return _decode_all(
        query_all(
            "SELECT * FROM digest WHERE job_seeker_id = ? ORDER BY period_end DESC LIMIT ?",
            (job_seeker_id, int(limit)),
        ),
        ("content", "recommended_action_detail"),
    )


# ---------------------------------------------------------------------------
# Inputs the digest and the schedulers read
# ---------------------------------------------------------------------------


#: How long a :func:`active_seeker_ids` answer is trusted.  The scheduler tick
#: is 60 s, so this is effectively "for the tick", while a manual run that
#: lands just after a tick still reuses that tick's answer.  A write to
#: campaign/watchlist/card/opportunity clears the cache immediately; a write
#: from another process cannot (no cross-process signal), which is what this
#: TTL bounds.
ACTIVE_SEEKER_CACHE_TTL_SECONDS = 60.0

#: ``(database, since-date) -> (monotonic seconds, ids)``.  The lock guards
#: the dict only; the query itself runs outside it so a slow first call never
#: serialises readers.
_active_seeker_cache: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
_active_seeker_cache_lock = threading.Lock()


def invalidate_active_seeker_cache() -> None:
    """Drop every memoised :func:`active_seeker_ids` answer.

    Called after writes to the four tables the query reads - campaigns,
    watchlist entries, pipeline cards and opportunities - and by tests.
    Clearing is deliberately the safe direction: a needless clear costs one
    query, a missed clear serves a stale one for up to the TTL.
    """
    with _active_seeker_cache_lock:
        _active_seeker_cache.clear()


def active_seeker_ids(since: str) -> list[str]:
    """Seekers with a live campaign, a watch or a live card (FR-403).

    "Active" has to mean something, or the weekly digest becomes a weekly
    e-mail to everyone who ever registered.

    The four-way UNION behind this reads ``campaign``, ``watchlist_entry``,
    ``pipeline_card`` and ``opportunity``; on the development database it was
    measured at up to 23.8 s and 735k steps per call, and both the follow-up
    sweep and the digest ask it on every tick.  The answer is memoised for
    :data:`ACTIVE_SEEKER_CACHE_TTL_SECONDS`, and every write to one of those
    tables clears the memo (see :func:`invalidate_active_seeker_cache`).

    The cache key includes only the *date* of ``since``, not the second it was
    computed from: the callers build ``since`` as "now minus N days" on every
    call, so keying on the full timestamp would miss on every tick and save
    nothing.  Serving an answer computed at most a minute ago is the accepted
    staleness - the window is 28 days for the digest and 90 for follow-ups, so
    a callback that crosses the boundary within that minute is immaterial.
    """
    db = str(get_settings().abs_db_path)
    key = (db, since[:10])
    now = time.monotonic()
    with _active_seeker_cache_lock:
        cached = _active_seeker_cache.get(key)
        if cached is not None and now - cached[0] < ACTIVE_SEEKER_CACHE_TTL_SECONDS:
            return list(cached[1])
    rows = query_all(
        """
        SELECT DISTINCT job_seeker_id FROM (
            SELECT job_seeker_id FROM campaign
             WHERE status IN ('planned', 'running', 'paused') OR created_at >= ?
            UNION SELECT job_seeker_id FROM watchlist_entry WHERE active = 1
            UNION SELECT job_seeker_id FROM pipeline_card WHERE stage <> 'closed'
            UNION SELECT job_seeker_id FROM opportunity WHERE updated_at >= ?
        )
        """,
        (since, since),
    )
    ids = tuple(r["job_seeker_id"] for r in rows if r["job_seeker_id"])
    with _active_seeker_cache_lock:
        _active_seeker_cache[key] = (time.monotonic(), ids)
    return list(ids)


def new_opportunities(job_seeker_id: str, since: str, limit: int = 25) -> list[dict]:
    return query_all(
        """
        SELECT o.id, o.title, o.kind, o.score, o.timing_flag, o.created_at, o.campaign_id,
               c.name AS company_name
        FROM opportunity o LEFT JOIN company c ON c.id = o.company_id
        WHERE o.job_seeker_id = ? AND o.created_at >= ? AND o.user_status <> 'not_interested'
        ORDER BY o.score DESC NULLS LAST, o.created_at DESC LIMIT ?
        """,
        (job_seeker_id, since, int(limit)),
    )


def watch_changes(job_seeker_id: str, since: str, limit: int = 50) -> list[dict]:
    """Notifications the watchlist raised in the period (FR-403)."""
    return query_all(
        "SELECT id, kind, title, body, payload, created_at FROM notification "
        "WHERE job_seeker_id = ? AND created_at >= ? AND kind IN ('new_vacancy', 'signal') "
        "ORDER BY created_at DESC LIMIT ?",
        (job_seeker_id, since, int(limit)),
    )


def due_follow_ups(job_seeker_id: str, before: str, limit: int = 50) -> list[dict]:
    """FR-327 follow-ups whose date has passed and which were never sent."""
    return query_all(
        """
        SELECT d.id AS dispatch_id, d.recipient_email, d.subject, d.follow_up_due_at,
               d.sent_at, p.opportunity_id, o.title AS opportunity_title,
               c.name AS company_name, k.id AS pipeline_card_id, k.stage
        FROM dispatch d
        JOIN application_package p ON p.id = d.application_package_id
        LEFT JOIN opportunity o ON o.id = p.opportunity_id
        LEFT JOIN company c ON c.id = o.company_id
        LEFT JOIN pipeline_card k ON k.application_package_id = p.id
        WHERE d.job_seeker_id = ? AND d.follow_up_due_at IS NOT NULL
          AND d.follow_up_due_at <= ? AND d.follow_up_sent_at IS NULL
          AND COALESCE(k.stage, 'sent') IN ('sent', 'replied')
        ORDER BY d.follow_up_due_at ASC LIMIT ?
        """,
        (job_seeker_id, before, int(limit)),
    )


def seeker(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT id, email, display_name, locale FROM job_seeker WHERE id = ?", (job_seeker_id,)
    )


def opportunity_context(opportunity_id: str, job_seeker_id: str) -> dict | None:
    """Everything the interview, negotiation and reply generators read.

    One statement rather than four: the caller has to prove ownership of the
    opportunity before any of the shared rows are reachable (FR-344).
    """
    return _decode(
        query_one(
            """
            SELECT o.*, c.name AS company_name, c.business_summary, c.products_services,
                   c.markets, c.values_culture, c.size_fte, c.size_band, c.stage AS company_stage,
                   c.trajectory AS company_trajectory, c.country AS company_country,
                   c.key_people, c.news, c.domain AS company_domain,
                   v.description AS vacancy_description, v.required_skills AS vacancy_required,
                   v.desirable_skills AS vacancy_desirable, v.source_url AS vacancy_url,
                   f.ability_to_pay, f.ability_to_pay_rationale, f.personnel_cost_per_fte,
                   f.trajectory AS financial_trajectory, f.revenue_cagr, f.headcount_cagr,
                   f.is_estimated AS financials_estimated
            FROM opportunity o
            LEFT JOIN company c ON c.id = o.company_id
            LEFT JOIN vacancy v ON v.id = o.vacancy_id
            LEFT JOIN financial_analysis f ON f.company_id = o.company_id
            WHERE o.id = ? AND o.job_seeker_id = ?
            """,
            (opportunity_id, job_seeker_id),
        ),
        (
            "required_skills", "desirable_skills", "products_services", "markets",
            "values_culture", "key_people", "news", "vacancy_required", "vacancy_desirable",
            "dream_fit_detail", "tags", "comp_sources",
        ),
    )


def package_for_opportunity(opportunity_id: str, job_seeker_id: str) -> dict | None:
    """The sent package, whose briefing and motivation documents are the context."""
    return query_one(
        "SELECT * FROM application_package WHERE opportunity_id = ? AND job_seeker_id = ? "
        "ORDER BY CASE status WHEN 'sent' THEN 0 WHEN 'approved' THEN 1 ELSE 2 END, "
        "created_at DESC LIMIT 1",
        (opportunity_id, job_seeker_id),
    )


def composite_profile(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM composite_profile WHERE job_seeker_id = ? ORDER BY version DESC LIMIT 1",
        (job_seeker_id,),
    )


def directives_for_campaign(campaign_id: str | None, job_seeker_id: str) -> dict | None:
    """The compensation directives the negotiation brief needs (FR-146, FR-444)."""
    if campaign_id:
        row = query_one(
            "SELECT d.* FROM directive_set d JOIN campaign k ON k.directive_set_id = d.id "
            "WHERE k.id = ? AND k.job_seeker_id = ?",
            (campaign_id, job_seeker_id),
        )
        if row:
            return row
    return query_one(
        "SELECT * FROM directive_set WHERE job_seeker_id = ? ORDER BY created_at DESC LIMIT 1",
        (job_seeker_id,),
    )
