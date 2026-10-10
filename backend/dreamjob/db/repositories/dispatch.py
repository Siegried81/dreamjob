"""Dispatch, mailbox and inbox SQL (FR-325, FR-326, FR-327, NFR-204, NFR-702).

Everything this slice reads or writes goes through this module (CR-408).

Three groups of tables meet here and the isolation rules differ:

``dispatch`` / ``incoming_reply`` (PRIVATE)
    The send log FR-326 asks for and the replies it is matched against.  Every
    statement filters on ``job_seeker_id``; reply matching in particular is
    scoped to the mailbox owner, so one job seeker's Message-ID can never
    resolve to another's dispatch (FR-101, FR-344).

``mail_account`` / ``mail_oauth_state`` / ``mail_poll_state`` (PRIVATE)
    Mailbox credentials.  ``credentials_enc`` only ever passes through here as
    an opaque blob - encryption and decryption live in ``mail.gmail`` and
    ``security.crypto`` (NFR-204).  :func:`list_accounts` never selects it, so
    the API surface cannot leak it by accident.

``contact`` / ``company`` / ``application_package`` (read-only joins)
    The dispatcher needs the recipient's address and validation verdict
    (FR-304), the company's country for the recipient time zone (FR-325) and
    the package's approval stamp (NFR-702).  It reads them, never writes them.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from dreamjob.db.connection import (
    execute,
    from_json,
    insert_row,
    query_all,
    query_one,
    to_json,
    update_row,
    upsert_row,
    utcnow,
    write_tx,
)
from dreamjob.db.repositories.pipeline_cards import invalidate_active_seeker_cache

#: Columns a caller may set on a dispatch.  Anything else is dropped rather
#: than silently written, so a stray key in a payload cannot rewrite the audit
#: fields (NFR-702).
_DISPATCH_COLUMNS = frozenset(
    {
        "application_package_id",
        "backend",
        "mail_account_id",
        "kind",
        "parent_dispatch_id",
        "recipient_email",
        "recipient_name",
        "subject",
        "message_id",
        "thread_id",
        "in_reply_to",
        "references_header",
        "attachments",
        "sent_at",
        "delivery_status",
        "delivery_detail",
        "bounce_detected_at",
        "reply_detected_at",
        "follow_up_due_at",
        "follow_up_sent_at",
        "follow_up_subject",
        "follow_up_draft",
        "scheduled_for",
        "recipient_timezone",
        "approved_by",
        "attempts",
        "last_error",
    }
)

_DISPATCH_JSON_COLUMNS = ("attachments", "delivery_detail")

#: The public view of a mailbox: never the credential blob (NFR-204).
_ACCOUNT_PUBLIC = (
    "id, job_seeker_id, backend, address, display_name, scopes, token_expires_at, "
    "is_active, connected_at, "
    "CASE WHEN credentials_enc IS NULL THEN 0 ELSE 1 END AS has_credentials"
)


def _decode(row: dict | None, json_columns: tuple[str, ...]) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for col in json_columns:
        if col in out:
            out[col] = from_json(out[col], None)
    return out


# ---------------------------------------------------------------------------
# Mailboxes (FR-325, NFR-204)
# ---------------------------------------------------------------------------


def list_accounts(job_seeker_id: str, *, include_inactive: bool = True) -> list[dict]:
    sql = f"SELECT {_ACCOUNT_PUBLIC} FROM mail_account WHERE job_seeker_id = ?"
    if not include_inactive:
        sql += " AND is_active = 1"
    sql += " ORDER BY connected_at DESC"
    return query_all(sql, (job_seeker_id,))


def get_account(account_id: str, job_seeker_id: str) -> dict | None:
    """The full row, credential blob included - callers that decrypt use this."""
    return query_one(
        "SELECT * FROM mail_account WHERE id = ? AND job_seeker_id = ?",
        (account_id, job_seeker_id),
    )


def get_account_any(account_id: str) -> dict | None:
    """For background polling, which runs after the request that owns it was authorised."""
    return query_one("SELECT * FROM mail_account WHERE id = ?", (account_id,))


def active_account(job_seeker_id: str, backend: str | None = None) -> dict | None:
    """The mailbox a send should leave from (FR-325: the job seeker's own)."""
    sql = "SELECT * FROM mail_account WHERE job_seeker_id = ? AND is_active = 1"
    params: list[Any] = [job_seeker_id]
    if backend:
        sql += " AND backend = ?"
        params.append(backend)
    # A connected personal mailbox outranks the shared relay: replies must land
    # in the job seeker's own inbox (section 2.4, RK-05).  Both personal
    # backends rank equally - gmail_oauth and imap_basic differ in how the
    # mailbox is authorised, not in whose inbox the replies reach - so between
    # two of them the most recently connected one wins.
    sql += (
        " ORDER BY CASE backend WHEN 'gmail_oauth' THEN 0 WHEN 'imap_basic' THEN 0 ELSE 1 END,"
        " connected_at DESC LIMIT 1"
    )
    return query_one(sql, tuple(params))


def accounts_to_poll(backend: str | Sequence[str] | None = None) -> list[dict]:
    """Connected mailboxes the inbox poller should read (FR-326).

    ``backend`` takes one key or several, because a job seeker may have a Gmail
    and an app-password mailbox connected at once and both are polled on one
    pass.  The caller decides which keys those are - it reads them from the
    backend capabilities - so this query never names a backend itself.
    """
    sql = "SELECT * FROM mail_account WHERE is_active = 1 AND credentials_enc IS NOT NULL"
    params: tuple = ()
    keys = [backend] if isinstance(backend, str) else list(backend or [])
    if keys:
        sql += f" AND backend IN ({', '.join('?' * len(keys))})"
        params = tuple(keys)
    return query_all(sql, params)


def upsert_account(
    job_seeker_id: str,
    *,
    backend: str,
    address: str,
    display_name: str | None = None,
    credentials_enc: bytes | None = None,
    scopes: str | None = None,
    token_expires_at: str | None = None,
) -> str:
    """Connect or reconnect a mailbox.  Returns the account id.

    ``UNIQUE (job_seeker_id, backend, address)`` makes reconnecting the same
    mailbox an update, so a re-authorisation replaces the token rather than
    leaving a second row behind holding a stale one (NFR-204).
    """
    address = address.strip().lower()
    existing = query_one(
        "SELECT id FROM mail_account WHERE job_seeker_id = ? AND backend = ? AND address = ?",
        (job_seeker_id, backend, address),
    )
    values: dict[str, Any] = {
        "display_name": display_name,
        "scopes": scopes,
        "token_expires_at": token_expires_at,
        "is_active": 1,
    }
    if credentials_enc is not None:
        values["credentials_enc"] = credentials_enc
    if existing:
        update_row("mail_account", existing["id"], values)
        return existing["id"]
    values.update(
        {
            "job_seeker_id": job_seeker_id,
            "backend": backend,
            "address": address,
            "connected_at": utcnow(),
        }
    )
    return insert_row("mail_account", values)


def set_account_credentials(
    account_id: str,
    credentials_enc: bytes,
    *,
    token_expires_at: str | None = None,
    scopes: str | None = None,
) -> None:
    values: dict[str, Any] = {"credentials_enc": credentials_enc}
    if token_expires_at is not None:
        values["token_expires_at"] = token_expires_at
    if scopes is not None:
        values["scopes"] = scopes
    update_row("mail_account", account_id, values)


def clear_credentials(account_id: str) -> None:
    """NFR-204: revocation wipes the stored token and takes the mailbox out of use."""
    update_row(
        "mail_account",
        account_id,
        {"credentials_enc": None, "token_expires_at": None, "is_active": 0},
    )


def delete_account(account_id: str, job_seeker_id: str) -> int:
    return execute(
        "DELETE FROM mail_account WHERE id = ? AND job_seeker_id = ?",
        (account_id, job_seeker_id),
    )


# ---------------------------------------------------------------------------
# OAuth handshake state (NFR-204)
# ---------------------------------------------------------------------------


def create_oauth_state(
    state: str,
    *,
    job_seeker_id: str,
    backend: str,
    code_verifier: str,
    redirect_uri: str,
    scopes: str,
    expires_at: str,
) -> None:
    # ``state`` is the primary key, so this table has no ``id`` column of its own.
    upsert_row(
        "mail_oauth_state",
        {
            "state": state,
            "job_seeker_id": job_seeker_id,
            "backend": backend,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "scopes": scopes,
            "created_at": utcnow(),
            "expires_at": expires_at,
        },
        ["state"],
    )


def take_oauth_state(state: str, *, now: str | None = None) -> dict | None:
    """Consume a state token.  Single use: the row is deleted as it is read.

    The read and the delete are one transaction.  As two, a replayed callback
    could read the row before the first request deleted it and both would be
    accepted - the state is exactly what stops a replayed redirect.
    """
    with write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM mail_oauth_state WHERE state = ?", (state,)
        ).fetchone()
        conn.execute("DELETE FROM mail_oauth_state WHERE state = ?", (state,))
    if row is None:
        return None
    consumed = dict(row)
    if consumed["expires_at"] <= (now or utcnow()):
        return None
    return consumed


def purge_expired_oauth_states(now: str | None = None) -> int:
    return execute("DELETE FROM mail_oauth_state WHERE expires_at <= ?", (now or utcnow(),))


# ---------------------------------------------------------------------------
# The send log (FR-326)
# ---------------------------------------------------------------------------


def create_dispatch(job_seeker_id: str, values: dict) -> str:
    payload = {k: v for k, v in values.items() if k in _DISPATCH_COLUMNS}
    payload["job_seeker_id"] = job_seeker_id
    payload.setdefault("created_at", utcnow())
    payload.setdefault("delivery_status", "queued")
    return insert_row("dispatch", payload)


def update_dispatch(dispatch_id: str, values: dict) -> None:
    payload = {k: v for k, v in values.items() if k in _DISPATCH_COLUMNS}
    if payload:
        update_row("dispatch", dispatch_id, payload)


def get_dispatch(dispatch_id: str, job_seeker_id: str) -> dict | None:
    return _decode(
        query_one(
            "SELECT * FROM dispatch WHERE id = ? AND job_seeker_id = ?",
            (dispatch_id, job_seeker_id),
        ),
        _DISPATCH_JSON_COLUMNS,
    )


def get_dispatch_any(dispatch_id: str) -> dict | None:
    return _decode(
        query_one("SELECT * FROM dispatch WHERE id = ?", (dispatch_id,)), _DISPATCH_JSON_COLUMNS
    )


def list_dispatches(
    job_seeker_id: str,
    *,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    sql = "SELECT * FROM dispatch WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if status:
        sql += " AND delivery_status = ?"
        params.append(status)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY COALESCE(sent_at, created_at) DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    rows = query_all(sql, tuple(params))
    return [d for d in (_decode(r, _DISPATCH_JSON_COLUMNS) for r in rows) if d]


def dispatches_for_package(package_id: str, job_seeker_id: str) -> list[dict]:
    rows = query_all(
        "SELECT * FROM dispatch WHERE application_package_id = ? AND job_seeker_id = ? "
        "ORDER BY created_at ASC",
        (package_id, job_seeker_id),
    )
    return [d for d in (_decode(r, _DISPATCH_JSON_COLUMNS) for r in rows) if d]


def follow_up_for(parent_dispatch_id: str) -> dict | None:
    """The follow-up already raised for one send, if any (FR-327: at most one)."""
    return _decode(
        query_one(
            "SELECT * FROM dispatch WHERE parent_dispatch_id = ? AND kind = 'follow_up' "
            "AND delivery_status <> 'failed' ORDER BY created_at DESC LIMIT 1",
            (parent_dispatch_id,),
        ),
        _DISPATCH_JSON_COLUMNS,
    )


def dispatch_by_message_id(job_seeker_id: str, message_id: str) -> dict | None:
    """Primary reply match: the Message-ID we generated came back in References."""
    return _decode(
        query_one(
            "SELECT * FROM dispatch WHERE job_seeker_id = ? AND message_id = ?",
            (job_seeker_id, message_id),
        ),
        _DISPATCH_JSON_COLUMNS,
    )


def dispatch_by_message_ids(job_seeker_id: str, message_ids: list[str]) -> dict | None:
    """Match against every id in In-Reply-To plus References, newest send first."""
    ids = [m for m in message_ids if m]
    if not ids:
        return None
    marks = ", ".join("?" for _ in ids)
    return _decode(
        query_one(
            f"SELECT * FROM dispatch WHERE job_seeker_id = ? AND message_id IN ({marks}) "
            "ORDER BY COALESCE(sent_at, created_at) DESC LIMIT 1",
            (job_seeker_id, *ids),
        ),
        _DISPATCH_JSON_COLUMNS,
    )


def dispatch_by_recipient(
    job_seeker_id: str, recipient_email: str, *, subject: str | None = None
) -> dict | None:
    """Fallback match when the headers were stripped: recipient, then subject.

    Mail systems that rewrite Message-IDs (and bounce reports that quote only
    the envelope) leave the address as the one reliable key, so the most recent
    send to that address wins, preferring one whose subject the reply echoes.
    """
    address = (recipient_email or "").strip().lower()
    if not address:
        return None
    if subject:
        core = normalise_subject(subject)
        row = query_one(
            "SELECT * FROM dispatch WHERE job_seeker_id = ? AND lower(recipient_email) = ? "
            "AND lower(COALESCE(subject, '')) = ? ORDER BY COALESCE(sent_at, created_at) DESC "
            "LIMIT 1",
            (job_seeker_id, address, core.lower()),
        )
        if row:
            return _decode(row, _DISPATCH_JSON_COLUMNS)
    return _decode(
        query_one(
            "SELECT * FROM dispatch WHERE job_seeker_id = ? AND lower(recipient_email) = ? "
            "AND sent_at IS NOT NULL ORDER BY sent_at DESC LIMIT 1",
            (job_seeker_id, address),
        ),
        _DISPATCH_JSON_COLUMNS,
    )


_SUBJECT_PREFIXES = (
    "re:", "re :", "aw:", "antw:", "antwoord:", "rép:", "rep:", "tr:", "fwd:", "fw:",
)


def normalise_subject(subject: str) -> str:
    """Strip reply/forward prefixes so 'Re: Re: X' and 'X' compare equal."""
    text = (subject or "").strip()
    changed = True
    while changed:
        changed = False
        lowered = text.lower()
        for prefix in _SUBJECT_PREFIXES:
            if lowered.startswith(prefix):
                text = text[len(prefix):].strip()
                changed = True
                break
    return text


# --- Guard-rail counters (FR-325, RK-05) -----------------------------------


def sent_since(job_seeker_id: str, since: str) -> int:
    """How many messages have actually left since ``since`` - the daily cap."""
    row = query_one(
        "SELECT COUNT(*) AS n FROM dispatch WHERE job_seeker_id = ? AND sent_at IS NOT NULL "
        "AND sent_at >= ?",
        (job_seeker_id, since),
    )
    return int((row or {}).get("n") or 0)


def last_sent_at(job_seeker_id: str) -> str | None:
    row = query_one(
        "SELECT MAX(sent_at) AS last FROM dispatch WHERE job_seeker_id = ? AND sent_at IS NOT NULL",
        (job_seeker_id,),
    )
    return (row or {}).get("last")


def already_sent_to(job_seeker_id: str, recipient_email: str, package_id: str) -> dict | None:
    """Guards against sending the same package to the same person twice.

    Only a dispatch that actually left counts (``sent_at`` is set).  A row that
    is merely ``queued`` for its send window - or a dry-run rehearsal - has not
    reached the recipient and must not block a later attempt as though it had.
    """
    return query_one(
        "SELECT id, sent_at, delivery_status, scheduled_for FROM dispatch "
        "WHERE job_seeker_id = ? AND lower(recipient_email) = ? "
        "AND application_package_id = ? AND sent_at IS NOT NULL "
        "ORDER BY sent_at DESC LIMIT 1",
        (job_seeker_id, recipient_email.strip().lower(), package_id),
    )


def supersede_pending(
    job_seeker_id: str, recipient_email: str, package_id: str, *, reason: str
) -> int:
    """Close dispatches that have not left, so a newer attempt can replace them.

    A ``queued`` row is a promise to send later and a ``dry_run`` row is a
    rehearsal; both are pending, neither has reached the recipient.  Leaving
    them pending would let the queue deliver the same package after the new
    attempt has already gone out, so they are marked ``failed`` with the reason
    and a ``superseded`` flag merged into ``delivery_detail`` (which is stored
    as JSON text, so it is decoded, merged and re-encoded).  Returns how many
    rows were changed.
    """
    rows = query_all(
        "SELECT id, delivery_detail FROM dispatch "
        "WHERE job_seeker_id = ? AND lower(recipient_email) = ? AND application_package_id = ? "
        "AND sent_at IS NULL AND delivery_status IN ('queued', 'dry_run', 'sending')",
        (job_seeker_id, recipient_email.strip().lower(), package_id),
    )
    changed = 0
    for row in rows:
        detail = from_json(row.get("delivery_detail"), None)
        if not isinstance(detail, dict):
            detail = {}
        detail["superseded"] = True
        update_row(
            "dispatch",
            row["id"],
            {
                "delivery_status": "failed",
                "last_error": reason,
                "delivery_detail": detail,
            },
        )
        changed += 1
    return changed


def due_queued(
    job_seeker_id: str | None = None, *, now: str | None = None, limit: int = 50
) -> list[dict]:
    """Queued sends whose send window has opened (FR-325)."""
    moment = now or utcnow()
    sql = (
        "SELECT * FROM dispatch WHERE delivery_status = 'queued' "
        "AND (scheduled_for IS NULL OR scheduled_for <= ?)"
    )
    params: list[Any] = [moment]
    if job_seeker_id:
        sql += " AND job_seeker_id = ?"
        params.append(job_seeker_id)
    sql += " ORDER BY created_at ASC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    return [d for d in (_decode(r, _DISPATCH_JSON_COLUMNS) for r in rows) if d]


def due_follow_ups(job_seeker_id: str | None = None, *, now: str | None = None) -> list[dict]:
    """Sent messages past their follow-up date with no reply and no bounce (FR-327)."""
    moment = now or utcnow()
    sql = (
        "SELECT * FROM dispatch WHERE follow_up_due_at IS NOT NULL AND follow_up_due_at <= ? "
        "AND follow_up_sent_at IS NULL AND reply_detected_at IS NULL "
        "AND bounce_detected_at IS NULL AND sent_at IS NOT NULL "
        "AND delivery_status IN ('sent', 'delivered')"
    )
    params: list[Any] = [moment]
    if job_seeker_id:
        sql += " AND job_seeker_id = ?"
        params.append(job_seeker_id)
    sql += " ORDER BY follow_up_due_at ASC"
    rows = query_all(sql, tuple(params))
    return [d for d in (_decode(r, _DISPATCH_JSON_COLUMNS) for r in rows) if d]


def status_counts(job_seeker_id: str) -> dict[str, int]:
    rows = query_all(
        "SELECT delivery_status, COUNT(*) AS n FROM dispatch WHERE job_seeker_id = ? "
        "GROUP BY delivery_status",
        (job_seeker_id,),
    )
    return {r["delivery_status"]: int(r["n"]) for r in rows}


# ---------------------------------------------------------------------------
# The material being sent (FR-321, FR-325, NFR-702)
# ---------------------------------------------------------------------------


def package_for_dispatch(package_id: str, job_seeker_id: str) -> dict | None:
    """One approved package with everything the dispatcher needs to decide.

    The company's country and locations come along because FR-325 expresses the
    send window in the recipient's time zone, and the contact's validation
    verdict because FR-304 forbids sending to an ``invalid`` address.
    """
    row = query_one(
        """
        SELECT p.*,
               o.title       AS opportunity_title,
               o.kind        AS opportunity_kind,
               o.language    AS opportunity_language,
               o.company_id  AS company_id,
               o.campaign_id AS campaign_id,
               c.name        AS company_name,
               c.country     AS company_country,
               c.jurisdiction AS company_jurisdiction,
               c.locations   AS company_locations,
               ct.full_name  AS contact_name,
               ct.role_title AS contact_role,
               ct.email      AS contact_email,
               ct.email_validation AS contact_email_validation,
               ct.objected   AS contact_objected,
               ct.is_generic_mailbox AS contact_is_generic
        FROM application_package p
        JOIN opportunity o ON o.id = p.opportunity_id
        LEFT JOIN company c  ON c.id = o.company_id
        LEFT JOIN contact ct ON ct.id = p.contact_id
        WHERE p.id = ? AND p.job_seeker_id = ?
        """,
        (package_id, job_seeker_id),
    )
    if row is None:
        return None
    out = dict(row)
    out["company_locations"] = from_json(out.get("company_locations"), []) or []
    out["consistency_report"] = from_json(out.get("consistency_report"), None)
    return out


def seeker_identity(job_seeker_id: str) -> dict | None:
    return query_one(
        "SELECT id, email, display_name, locale FROM job_seeker WHERE id = ?", (job_seeker_id,)
    )


def mark_package_sent(package_id: str, job_seeker_id: str) -> None:
    execute(
        "UPDATE application_package SET status = 'sent', updated_at = ? "
        "WHERE id = ? AND job_seeker_id = ?",
        (utcnow(), package_id, job_seeker_id),
    )


def mark_opportunity_applied(opportunity_id: str, job_seeker_id: str) -> None:
    """FR-326: dispatch is reflected in the opportunity status."""
    changed = execute(
        "UPDATE opportunity SET user_status = 'applied', updated_at = ? "
        "WHERE id = ? AND job_seeker_id = ? AND user_status <> 'applied'",
        (utcnow(), opportunity_id, job_seeker_id),
    )
    if changed:
        invalidate_active_seeker_cache()


def contact_email_state(email: str) -> dict | None:
    """FR-304 / NFR-302 verdict for a bare address, whichever contact holds it."""
    return query_one(
        "SELECT id, email, email_validation, objected FROM contact WHERE lower(email) = ? "
        "ORDER BY objected DESC LIMIT 1",
        ((email or "").strip().lower(),),
    )


# ---------------------------------------------------------------------------
# Incoming mail (FR-326)
# ---------------------------------------------------------------------------


def reply_exists(job_seeker_id: str, message_id: str) -> bool:
    if not message_id:
        return False
    return (
        query_one(
            "SELECT 1 AS hit FROM incoming_reply WHERE job_seeker_id = ? AND message_id = ?",
            (job_seeker_id, message_id),
        )
        is not None
    )


def record_reply(job_seeker_id: str, values: dict) -> str:
    payload = {
        "job_seeker_id": job_seeker_id,
        "dispatch_id": values.get("dispatch_id"),
        "from_address": values.get("from_address"),
        "subject": values.get("subject"),
        "body": values.get("body"),
        "received_at": values.get("received_at") or utcnow(),
        "message_id": values.get("message_id"),
        "in_reply_to": values.get("in_reply_to"),
        "classification": values.get("classification"),
        "classification_confidence": values.get("classification_confidence"),
        "extracted_slots": to_json(values.get("extracted_slots"))
        if values.get("extracted_slots")
        else None,
        "created_at": utcnow(),
    }
    return insert_row("incoming_reply", payload)


def list_replies(
    job_seeker_id: str, *, handled: bool | None = None, limit: int = 100
) -> list[dict]:
    sql = "SELECT * FROM incoming_reply WHERE job_seeker_id = ?"
    params: list[Any] = [job_seeker_id]
    if handled is not None:
        sql += " AND handled = ?"
        params.append(1 if handled else 0)
    sql += " ORDER BY COALESCE(received_at, created_at) DESC LIMIT ?"
    params.append(limit)
    rows = query_all(sql, tuple(params))
    out = []
    for row in rows:
        item = dict(row)
        item["extracted_slots"] = from_json(item.get("extracted_slots"), None)
        out.append(item)
    return out


def notify(
    job_seeker_id: str,
    kind: str,
    title: str,
    body: str | None,
    payload: dict,
    *,
    dedup_key: str | None = None,
) -> str | None:
    """Insert one notification; a repeat of the same ``dedup_key`` is dropped.

    The unique index on ``(job_seeker_id, kind, dedup_key)`` does the work, so
    a reminder that falls due stays one reminder however often the queue or
    the scheduler looks at it (FR-327).
    """
    values: dict[str, Any] = {
        "job_seeker_id": job_seeker_id,
        "kind": kind,
        "title": title[:200],
        "body": body,
        "payload": to_json(payload),
        "created_at": utcnow(),
    }
    if dedup_key:
        values["dedup_key"] = dedup_key
    try:
        return insert_row("notification", values)
    except Exception:  # noqa: BLE001 - a repeated reminder is not an error
        return None


# --- Poll cursors and webhook receipts (FR-326) ----------------------------


def get_poll_state(mail_account_id: str) -> dict | None:
    return query_one(
        "SELECT * FROM mail_poll_state WHERE mail_account_id = ?", (mail_account_id,)
    )


def save_poll_state(mail_account_id: str, values: dict) -> None:
    payload = {k: v for k, v in values.items() if k != "mail_account_id"}
    payload["mail_account_id"] = mail_account_id
    payload.setdefault("last_polled_at", utcnow())
    upsert_row("mail_poll_state", payload, ["mail_account_id"])


def webhook_event_seen(event_id: str) -> bool:
    row = query_one("SELECT 1 AS hit FROM mail_webhook_event WHERE id = ?", (event_id,))
    return row is not None


def record_webhook_event(
    event_id: str,
    *,
    provider: str,
    event_type: str,
    provider_message_id: str | None,
    recipient: str | None,
    dispatch_id: str | None,
    payload: Any,
) -> str:
    upsert_row(
        "mail_webhook_event",
        {
            "id": event_id,
            "provider": provider,
            "event_type": event_type,
            "provider_message_id": provider_message_id,
            "recipient": recipient,
            "dispatch_id": dispatch_id,
            "payload": to_json(payload),
            "received_at": utcnow(),
        },
        ["id"],
    )
    return event_id


def mark_webhook_processed(event_id: str, dispatch_id: str | None = None) -> None:
    update_row(
        "mail_webhook_event",
        event_id,
        {"processed_at": utcnow(), **({"dispatch_id": dispatch_id} if dispatch_id else {})},
    )


def dispatch_by_provider_message_id(provider_message_id: str) -> dict | None:
    """Resend returns its own id at send time; it is stored in ``thread_id``."""
    return _decode(
        query_one(
            "SELECT * FROM dispatch WHERE thread_id = ? ORDER BY created_at DESC LIMIT 1",
            (provider_message_id,),
        ),
        _DISPATCH_JSON_COLUMNS,
    )


# ---------------------------------------------------------------------------
# Settings that must be rotatable without a redeploy (webhook secret)
# ---------------------------------------------------------------------------


def get_setting(key: str) -> str | None:
    row = query_one("SELECT value FROM app_setting WHERE key = ?", (key,))
    return (row or {}).get("value")


def set_setting(key: str, value: str) -> None:
    upsert_row("app_setting", {"key": key, "value": value, "updated_at": utcnow()}, ["key"])
