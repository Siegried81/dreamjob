"""Mail connection, dispatch and inbox API (FR-325, FR-326, FR-327, NFR-204, NFR-302).

Four surfaces:

* ``/status``, ``/accounts``, ``/gmail/*``, ``/imap/*`` - connecting and
  disconnecting a mailbox.  ``/gmail/callback`` is the loopback redirect target
  and is the one route here without a session: the browser arrives from Google,
  and the single-use ``state`` row is what identifies the job seeker (NFR-204).
  ``/imap/connect`` is the equivalent for a provider with no OAuth programme -
  Yahoo, iCloud, a self-hosted server - which issues an application password
  instead; it verifies the credential against the provider before storing it.
  ``/accounts/{id}/revoke`` is the "revocable from the UI" half of NFR-204.
* ``/send``, ``/send-check``, ``/queue/process`` - dispatch.  Every guard rail
  lives in :mod:`dreamjob.mail.dispatcher`, so a client cannot talk its way
  past a send window or a daily cap by calling the API directly (FR-325).
* ``/dispatches``, ``/replies``, ``/poll`` - the send log and what came back.
* ``/resend/webhook`` - unauthenticated by necessity and verified by signature,
  because Resend has no mailbox to poll (FR-326).
"""

from __future__ import annotations

from html import escape
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
from dreamjob.db.repositories import dispatch as repo
from dreamjob.mail import gmail, imap_basic, inbox, resend_backend
from dreamjob.mail.base import (
    MailBackendError,
    MailBackendNotConfigured,
    MailBackendUnavailable,
    all_capabilities,
    get_backend,
)
from dreamjob.mail.dispatcher import (
    SendRefused,
    check_send_allowed,
    due_follow_ups,
    follow_up_days,
    generate_follow_up,
    process_queue,
    raise_follow_up_notifications,
    send_follow_up,
    send_package,
    set_follow_up_days,
)
from dreamjob.security.audit import record_audit

router = APIRouter()


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class SendIn(BaseModel):
    application_package_id: str
    backend: str | None = Field(
        None, description="gmail_oauth | imap_basic | resend; default: the connected mailbox"
    )
    ignore_window: bool = Field(
        False, description="Send outside the recipient's working hours (FR-325 override)"
    )


class FollowUpIn(BaseModel):
    subject: str | None = None
    body: str | None = None


class MailSettingsIn(BaseModel):
    follow_up_days: int = Field(7, ge=1, le=90)


class WebhookSecretIn(BaseModel):
    secret: str = Field(min_length=8, max_length=200)


class ImapConnectIn(BaseModel):
    """Connect a mailbox that authenticates with an application password.

    ``provider`` fills the hosts from a preset; ``imap_host``/``smtp_host``
    override it, so a self-hosted server needs no preset.  The password is
    never echoed back - not in the response, not in the audit detail.
    """

    address: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512, description="Provider app password")
    provider: str | None = Field(None, description="yahoo | icloud | outlook | gmail")
    imap_host: str | None = None
    imap_port: int | None = Field(None, ge=1, le=65535)
    smtp_host: str | None = None
    smtp_port: int | None = Field(None, ge=1, le=65535)
    folder: str = Field("INBOX", max_length=200, description="IMAP folder the poller reads")


def _bad_request(exc: Exception) -> HTTPException:
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(exc))


# ---------------------------------------------------------------------------
# Status and mailboxes (FR-325, NFR-204)
# ---------------------------------------------------------------------------


@router.get("/status")
def mail_status(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """What each backend can do and whether it is usable right now.

    Resend reports ``configured: false`` with the reason while ``RESEND_API_KEY``
    is missing, rather than looking healthy until the first send fails.
    """
    accounts = repo.list_accounts(seeker.id)
    by_backend = {a["backend"]: a for a in accounts if a["is_active"]}
    backends = []
    for capability in all_capabilities():
        account = by_backend.get(capability.key)
        full = repo.get_account(account["id"], seeker.id) if account else None
        backends.append(get_backend(capability.key, full).status())
    return {
        "backends": backends,
        "accounts": accounts,
        "dispatch_counts": repo.status_counts(seeker.id),
        "follow_up_days": follow_up_days(),
    }


@router.get("/accounts")
def list_accounts(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return repo.list_accounts(seeker.id)


@router.post("/accounts/{account_id}/revoke")
def revoke_account(account_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """NFR-204: revoke the token at Google and wipe the stored credential."""
    account = repo.get_account(account_id, seeker.id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    if account["backend"] == "gmail_oauth":
        result: dict[str, Any] = gmail.revoke(account)
    elif account["backend"] == imap_basic.BACKEND_KEY:
        # An app password has no revocation endpoint; the result carries the
        # provider page where the job seeker finishes the job themselves.
        result = imap_basic.disconnect(account)
    else:
        repo.clear_credentials(account_id)
        result = {"revoked_locally": True, "revoked_at_google": False}
    record_audit(
        "mail_account.revoked",
        "mail_account",
        account_id,
        seeker_id=seeker.id,
        detail={"address": account["address"], **result},
    )
    return result


@router.delete("/accounts/{account_id}")
def delete_account(account_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Disconnect and forget the mailbox.  Revokes first, so no token is orphaned."""
    account = repo.get_account(account_id, seeker.id)
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Mailbox not found")
    if account.get("credentials_enc"):
        if account["backend"] == "gmail_oauth":
            gmail.revoke(account)
        elif account["backend"] == imap_basic.BACKEND_KEY:
            imap_basic.disconnect(account)
    repo.delete_account(account_id, seeker.id)
    record_audit(
        "mail_account.deleted",
        "mail_account",
        account_id,
        seeker_id=seeker.id,
        detail={"address": account["address"]},
    )
    return {"deleted": True}


# --- Gmail OAuth (NFR-204) -------------------------------------------------


# --- IMAP + app password (FR-325, NFR-204) --------------------------------


@router.get("/imap/providers")
def imap_providers() -> dict:
    """Presets for the connect form, each with the page that issues the password."""
    return {
        "providers": imap_basic.provider_choices(),
        "default_imap_port": imap_basic.DEFAULT_IMAP_PORT,
        "default_smtp_port": imap_basic.DEFAULT_SMTP_PORT,
    }


@router.post("/imap/connect")
def imap_connect(
    payload: ImapConnectIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Verify an application password against the provider, then store it.

    Verification is not optional here: a credential that only fails at the next
    poll makes a mailbox that looks connected and reads nothing, because the
    poller swallows per-account errors so one bad mailbox cannot stop the rest.
    """
    try:
        account = imap_basic.connect(
            seeker.id,
            address=payload.address,
            password=payload.password,
            provider=payload.provider,
            imap_host=payload.imap_host,
            imap_port=payload.imap_port,
            smtp_host=payload.smtp_host,
            smtp_port=payload.smtp_port,
            folder=payload.folder,
        )
    except MailBackendError as exc:
        raise _bad_request(exc) from exc
    record_audit(
        "mail_account.connected",
        "mail_account",
        account["id"],
        seeker_id=seeker.id,
        # The password is deliberately absent: an audit row is readable by an
        # admin, and NFR-204 keeps the credential to the encrypted blob only.
        detail={
            "backend": account["backend"],
            "address": account["address"],
            "provider": account.get("provider"),
            "imap_host": account.get("imap_host"),
            "folder": account.get("folder"),
        },
    )
    return {**account, "sending_armed": imap_basic.sending_is_armed()}


@router.get("/gmail/authorize")
def gmail_authorize(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Begin the authorisation-code flow; the client opens the returned URL."""
    try:
        return gmail.authorization_url(seeker.id, login_hint=seeker.email)
    except MailBackendNotConfigured as exc:
        raise _bad_request(exc) from exc


@router.get("/gmail/callback", include_in_schema=False)
def gmail_callback(
    code: str | None = None, state: str | None = None, error: str | None = None
) -> HTMLResponse:
    """The loopback redirect target.

    No session dependency: the browser arrives here from Google, not from the
    application, and the single-use ``state`` row is what binds the redirect to
    the job seeker who started it.
    """
    if error:
        return _callback_page(f"Google reported: {error}", ok=False)
    if not code or not state:
        return _callback_page("The callback arrived without a code or state.", ok=False)
    try:
        account = gmail.complete_authorization(state, code)
    except MailBackendError as exc:
        return _callback_page(str(exc), ok=False)
    record_audit(
        "mail_account.connected",
        "mail_account",
        account["id"],
        seeker_id=account["job_seeker_id"],
        detail={"address": account["address"], "scopes": account["scopes"]},
    )
    return _callback_page(f"{account['address']} is connected. You can close this tab.", ok=True)


def _callback_page(message: str, *, ok: bool) -> HTMLResponse:
    colour = "#1a7f37" if ok else "#b42318"
    title = "Mailbox connected" if ok else "Connection failed"
    # The message can carry a provider error string straight from the query
    # string, and this page is served on the API origin with the session cookie
    # in scope - escaping is the whole defence against a crafted callback link.
    safe_message = escape(str(message))
    body = (
        "<!doctype html><meta charset='utf-8'><title>Dream Job</title>"
        "<body style=\"font:16px/1.5 system-ui,sans-serif;margin:4rem auto;max-width:34rem\">"
        f"<h1 style='color:{colour};font-size:1.3rem'>{title}</h1><p>{safe_message}</p></body>"
    )
    return HTMLResponse(body, status_code=200 if ok else 400)


# --- Resend (FR-326) -------------------------------------------------------


@router.post("/resend/webhook", include_in_schema=False)
async def resend_webhook(request: Request) -> Response:
    """Delivery, bounce and complaint events.

    Unauthenticated by necessity - Resend has no session with us - so the
    signature over the raw body is the whole of the authentication, and the
    body must be read raw rather than re-serialised from parsed JSON.
    """
    body = await request.body()
    try:
        result = resend_backend.handle_event(body, dict(request.headers))
    except resend_backend.WebhookVerificationError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    # A 2xx stops the provider's retries; anything else has it deliver again.
    return Response(content=None, status_code=status.HTTP_204_NO_CONTENT, headers={
        "X-Dreamjob-Result": str(result.get("status", "ok"))
    })


@router.put("/resend/webhook-secret")
def set_resend_webhook_secret(
    payload: WebhookSecretIn, admin: CurrentSeeker = Depends(current_admin)
) -> dict:
    """Store the signing secret; administrator only, and never read back."""
    resend_backend.set_webhook_secret(payload.secret)
    record_audit("mail.webhook_secret_set", "app_setting", "resend", seeker_id=admin.id)
    return {"configured": True}


# ---------------------------------------------------------------------------
# Dispatch (FR-325, FR-326, NFR-702)
# ---------------------------------------------------------------------------


@router.get("/send-check")
def send_check(
    application_package_id: str, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    """Dry-run the guard rails, so the preview screen can say why a send waits."""
    package = repo.package_for_dispatch(application_package_id, seeker.id)
    if package is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Application package not found")
    decision = check_send_allowed(
        seeker.id,
        recipient_email=package.get("contact_email"),
        email_validation=package.get("contact_email_validation"),
        objected=bool(package.get("contact_objected")),
        country=package.get("company_country"),
        locations=package.get("company_locations"),
        jurisdiction=package.get("company_jurisdiction"),
    )
    return {
        "allowed": decision.allowed,
        "code": decision.code,
        "reason": decision.reason,
        "retry_at": decision.retry_at,
        "timezone": decision.timezone,
        "recipient_local_time": decision.recipient_local_time,
        "checks": decision.checks,
        "recipient": package.get("contact_email"),
        "package_status": package.get("status"),
    }


@router.post("/send", status_code=status.HTTP_201_CREATED)
def send(payload: SendIn, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Send an approved package, or queue it until its send window opens."""
    try:
        return send_package(
            payload.application_package_id,
            seeker.id,
            approved_by=seeker.id,
            backend_key=payload.backend,
            ignore_window=payload.ignore_window,
        )
    # Only the provider failing is a gateway error.  A refusal, a missing
    # credential and an attachment FR-321 forbids are all things the job seeker
    # can act on, and reporting them as 502 would send them looking for an
    # outage that is not there.
    except MailBackendUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except MailBackendError as exc:
        raise _bad_request(exc) from exc


@router.post("/queue/process")
def process_send_queue(
    limit: int = Query(25, ge=1, le=200), seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    return process_queue(seeker.id, limit=limit)


@router.get("/dispatches")
def list_dispatches(
    dispatch_status: str | None = Query(None, alias="status"),
    kind: str | None = None,
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> list[dict]:
    """The FR-326 send log: recipient, timestamp, attachments, id and status."""
    return repo.list_dispatches(
        seeker.id, status=dispatch_status, kind=kind, limit=limit, offset=offset
    )


@router.get("/dispatches/{dispatch_id}")
def get_dispatch(dispatch_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    row = repo.get_dispatch(dispatch_id, seeker.id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Dispatch not found")
    return row


# ---------------------------------------------------------------------------
# Follow-ups (FR-327)
# ---------------------------------------------------------------------------


@router.get("/follow-ups")
def list_follow_ups(seeker: CurrentSeeker = Depends(current_seeker)) -> list[dict]:
    return due_follow_ups(seeker.id)


@router.post("/follow-ups/remind")
def remind_follow_ups(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    return {"notifications": raise_follow_up_notifications(seeker.id)}


@router.post("/dispatches/{dispatch_id}/follow-up")
def draft_follow_up(dispatch_id: str, seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    """Generate the optional follow-up e-mail for review (FR-327, FR-324)."""
    try:
        return generate_follow_up(dispatch_id, seeker.id)
    except SendRefused as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc


@router.post("/dispatches/{dispatch_id}/follow-up/send", status_code=status.HTTP_201_CREATED)
def dispatch_follow_up(
    dispatch_id: str,
    payload: FollowUpIn,
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    try:
        return send_follow_up(
            dispatch_id,
            seeker.id,
            approved_by=seeker.id,
            subject=payload.subject,
            body=payload.body,
        )
    except MailBackendUnavailable as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
    except MailBackendError as exc:
        raise _bad_request(exc) from exc


# ---------------------------------------------------------------------------
# Inbox (FR-326)
# ---------------------------------------------------------------------------


@router.post("/poll")
def poll_inbox(
    max_messages: int = Query(100, ge=1, le=500),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> dict:
    """Read the connected mailbox for replies and bounces."""
    return {"accounts": inbox.poll_all(seeker.id, max_messages=max_messages)}


@router.get("/replies")
def list_replies(
    handled: bool | None = None,
    limit: int = Query(100, ge=1, le=500),
    seeker: CurrentSeeker = Depends(current_seeker),
) -> list[dict]:
    return repo.list_replies(seeker.id, handled=handled, limit=limit)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@router.get("/settings")
def get_mail_settings(seeker: CurrentSeeker = Depends(current_seeker)) -> dict:
    from dreamjob.config import get_settings  # noqa: PLC0415

    s = get_settings()
    return {
        "follow_up_days": follow_up_days(),
        "send_daily_cap": s.send_daily_cap,
        "send_min_interval_seconds": s.send_min_interval_seconds,
        "send_window_start": s.send_window_start.strftime("%H:%M"),
        "send_window_end": s.send_window_end.strftime("%H:%M"),
        "window_timezone": "recipient",
        "default_backend": s.mail_backend,
    }


@router.put("/settings")
def update_mail_settings(
    payload: MailSettingsIn, seeker: CurrentSeeker = Depends(current_seeker)
) -> dict:
    return {"follow_up_days": set_follow_up_days(payload.follow_up_days)}
