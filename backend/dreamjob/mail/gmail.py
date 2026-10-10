"""Gmail over OAuth 2.0 (FR-325, FR-326, NFR-204).

This is the backend that satisfies section 2.4 in full: the message leaves the
job seeker's own mailbox, so replies and delivery-status notifications arrive
in their inbox rather than in a relay's.

Flow
----
Authorisation code with PKCE and a loopback redirect
(``settings.gmail_redirect_uri``, ``http://127.0.0.1:8000/api/mail/gmail/callback``
by default).  Dream Job is a locally installed application, so this is the flow
Google prescribes for it: there is no front channel on a public host to protect,
the client secret is not a secret in an installed app, and PKCE plus a
single-use ``state`` row are what actually bind the redirect back to the
request that started it.

Scopes (NFR-204)
----------------
``gmail.send`` + ``gmail.readonly``.

``gmail.send`` is exactly "send only, no read".  For the read half there is no
narrower published scope that works: Google offers ``gmail.readonly``,
``gmail.metadata`` and ``gmail.modify``, and no "read bounces only" scope
exists.  ``gmail.metadata`` is tempting - it is narrower - but it refuses
``format=full`` and ``format=raw``, and a delivery-status notification carries
its verdict in a ``message/delivery-status`` body part (FR-326), not in its
headers, so metadata access cannot tell a hard bounce from an out-of-office.
``gmail.modify`` is strictly wider (it can delete).  ``gmail.readonly`` is
therefore the narrowest pair member that can do the job.  Note also that IMAP
access over XOAUTH2 would require ``https://mail.google.com/`` - full mailbox
control - which is why the Gmail API is the default reading path here and
imaplib is used only for mailboxes that already grant it (see ``mail.inbox``).

NFR-204 is then met by the three things that are in our control: the refresh
token is encrypted at rest with a key derived per job seeker
(``crypto.encrypt(purpose="mail", scope=<seeker id>)``), the token is used only
by this module, and :func:`revoke` calls Google's revocation endpoint and wipes
the stored credential so revocation from the UI is real and not cosmetic.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import Any, ClassVar

import httpx

from dreamjob.config import get_settings
from dreamjob.db.repositories import dispatch as repo
from dreamjob.mail.base import CRYPTO_PURPOSE as BASE_CRYPTO_PURPOSE
from dreamjob.mail.base import (
    BackendCapabilities,
    MailBackend,
    MailBackendError,
    MailBackendNotConfigured,
    MailBackendUnavailable,
    OutgoingMessage,
    SendResult,
    decrypt_credentials,
    encrypt_credentials,
    register_backend,
)

log = logging.getLogger(__name__)

AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
REVOKE_ENDPOINT = "https://oauth2.googleapis.com/revoke"
API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
UPLOAD_ROOT = "https://gmail.googleapis.com/upload/gmail/v1/users/me"

#: NFR-204 - see the module docstring for why a narrower pair is not available.
SCOPES: tuple[str, ...] = (
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/gmail.readonly",
)
#: The scope IMAP (XOAUTH2) needs.  Never requested; recognised if already granted.
IMAP_SCOPE = "https://mail.google.com/"

#: Kept as an alias: the purpose is defined once, in :mod:`dreamjob.mail.base`.
CRYPTO_PURPOSE = BASE_CRYPTO_PURPOSE
STATE_TTL_SECONDS = 600
#: Refresh a little early, so a long send does not straddle the expiry.
EXPIRY_SKEW_SECONDS = 120
#: Above this the Gmail API wants the media upload endpoint rather than JSON.
SIMPLE_SEND_LIMIT = 4 * 1024 * 1024


class GmailAuthError(MailBackendError):
    """The OAuth handshake or a token refresh failed."""


# ---------------------------------------------------------------------------
# Credential envelope (NFR-204)
# ---------------------------------------------------------------------------


# The envelope itself lives in :mod:`dreamjob.mail.base` so that every backend
# seals its credential the same way; these two wrappers only name Gmail in the
# error text, which is what the job seeker reads.
def _encrypt_credentials(credentials: dict, job_seeker_id: str) -> bytes:
    return encrypt_credentials(credentials, job_seeker_id, what="Gmail mailbox")


def _decrypt_credentials(blob: bytes | None, job_seeker_id: str) -> dict:
    return decrypt_credentials(
        blob,
        job_seeker_id,
        what="Gmail mailbox",
        reconnect_hint="Open Settings > Mail and connect it again.",
    )


# ---------------------------------------------------------------------------
# Authorisation code flow with PKCE
# ---------------------------------------------------------------------------


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:128]
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .decode("ascii")
        .rstrip("=")
    )
    return verifier, challenge


def authorization_url(job_seeker_id: str, *, login_hint: str | None = None) -> dict:
    """Start the handshake.  Returns the URL to open and the state it is bound to."""
    s = get_settings()
    if not s.gmail_client_id:
        raise MailBackendNotConfigured(
            "Gmail is not set up on this installation: GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET "
            "are empty. Create an OAuth client of type 'Desktop app' in the Google Cloud console, "
            f"add {s.gmail_redirect_uri} as an authorised redirect URI, and put the two values "
            "in .env."
        )

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    scope = " ".join(SCOPES)
    repo.create_oauth_state(
        state,
        job_seeker_id=job_seeker_id,
        backend="gmail_oauth",
        code_verifier=verifier,
        redirect_uri=s.gmail_redirect_uri,
        scopes=scope,
        expires_at=(datetime.now(UTC) + timedelta(seconds=STATE_TTL_SECONDS)).isoformat(
            timespec="seconds"
        ),
    )
    params = {
        "client_id": s.gmail_client_id,
        "redirect_uri": s.gmail_redirect_uri,
        "response_type": "code",
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        # offline + consent are what produce a refresh token on every run; without
        # them a re-authorisation returns an access token only and the mailbox
        # stops working an hour later.
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "false",
    }
    if login_hint:
        params["login_hint"] = login_hint
    url = str(httpx.URL(AUTH_ENDPOINT, params=params))
    log.debug("Built a Gmail authorisation URL for seeker %s", job_seeker_id)
    return {"authorization_url": url, "state": state, "scopes": list(SCOPES)}


def _token_request(payload: dict) -> dict:
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(TOKEN_ENDPOINT, data=payload)
    except httpx.HTTPError as exc:
        raise MailBackendUnavailable(f"Could not reach Google's token endpoint: {exc}") from exc
    if resp.status_code >= 400:
        detail = ""
        try:
            body = resp.json()
            detail = f"{body.get('error')}: {body.get('error_description', '')}".strip(": ")
        except ValueError:
            detail = resp.text[:300]
        raise GmailAuthError(f"Google rejected the token request ({resp.status_code}): {detail}")
    return resp.json()


def _expiry_iso(expires_in: int | None) -> str | None:
    if not expires_in:
        return None
    return (datetime.now(UTC) + timedelta(seconds=int(expires_in))).isoformat(timespec="seconds")


def complete_authorization(state: str, code: str) -> dict:
    """Finish the loopback redirect: exchange the code and store the mailbox.

    Returns the connected account row (without its credential blob).
    """
    row = repo.take_oauth_state(state)
    if row is None:
        raise GmailAuthError(
            "This authorisation link has expired or was already used. Start the connection again."
        )
    s = get_settings()
    tokens = _token_request(
        {
            "client_id": s.gmail_client_id,
            "client_secret": s.gmail_client_secret,
            "code": code,
            "code_verifier": row["code_verifier"],
            "grant_type": "authorization_code",
            "redirect_uri": row["redirect_uri"],
        }
    )
    refresh_token = tokens.get("refresh_token")
    if not refresh_token:
        raise GmailAuthError(
            "Google returned no refresh token. Remove Dream Job from "
            "https://myaccount.google.com/permissions and connect the mailbox again."
        )

    access_token = tokens.get("access_token", "")
    address = _profile_address(access_token)
    granted = tokens.get("scope") or row["scopes"] or " ".join(SCOPES)
    credentials = {
        "refresh_token": refresh_token,
        "access_token": access_token,
        "access_token_expires_at": _expiry_iso(tokens.get("expires_in")),
        "token_type": tokens.get("token_type", "Bearer"),
        "scope": granted,
    }
    account_id = repo.upsert_account(
        row["job_seeker_id"],
        backend="gmail_oauth",
        address=address,
        credentials_enc=_encrypt_credentials(credentials, row["job_seeker_id"]),
        scopes=granted,
        token_expires_at=credentials["access_token_expires_at"],
    )
    log.info("Connected Gmail mailbox %s for seeker %s", address, row["job_seeker_id"])
    return {
        "id": account_id,
        "job_seeker_id": row["job_seeker_id"],
        "backend": "gmail_oauth",
        "address": address,
        "scopes": granted,
    }


def _profile_address(access_token: str) -> str:
    try:
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(
                f"{API_ROOT}/profile", headers={"Authorization": f"Bearer {access_token}"}
            )
        resp.raise_for_status()
        return str(resp.json().get("emailAddress", "")).lower()
    except httpx.HTTPError as exc:
        raise GmailAuthError(f"Could not read the Gmail profile after authorising: {exc}") from exc


def revoke(account: dict) -> dict:
    """NFR-204: revocable from the UI, and revoked at Google as well as here.

    The local credential is wiped even when Google's endpoint is unreachable -
    a token we no longer hold cannot be used from this installation, and the
    result says whether the remote half succeeded so the UI can tell the job
    seeker to finish the job at myaccount.google.com if it did not.
    """
    remote_ok = False
    error: str | None = None
    try:
        credentials = _decrypt_credentials(account.get("credentials_enc"), account["job_seeker_id"])
        token = credentials.get("refresh_token") or credentials.get("access_token")
        if token:
            with httpx.Client(timeout=20.0) as client:
                resp = client.post(REVOKE_ENDPOINT, data={"token": token})
            # 200 means revoked; 400 "invalid_token" means it already was.
            remote_ok = resp.status_code == 200 or "invalid_token" in resp.text
            if not remote_ok:
                error = f"Google returned {resp.status_code}: {resp.text[:200]}"
    except (MailBackendError, httpx.HTTPError) as exc:
        error = str(exc)
    finally:
        repo.clear_credentials(account["id"])

    return {"revoked_locally": True, "revoked_at_google": remote_ok, "error": error}


# ---------------------------------------------------------------------------
# The backend
# ---------------------------------------------------------------------------


@register_backend
class GmailBackend(MailBackend):
    """Send through ``users.messages.send`` as the job seeker."""

    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        key="gmail_oauth",
        display_name="Gmail (OAuth)",
        replies_to_seeker_inbox=True,
        bounce_detection="gmail_api",
        reply_detection="poll",
        requires_oauth=True,
        max_message_bytes=25 * 1024 * 1024,
    )

    def __init__(self, account: dict | None = None, *, job_seeker_id: str | None = None):
        super().__init__(account, job_seeker_id=job_seeker_id)
        self._credentials: dict | None = None

    # -- credentials ------------------------------------------------------
    @property
    def credentials(self) -> dict:
        if self._credentials is None:
            if not self.account:
                raise MailBackendNotConfigured(
                    "No Gmail mailbox is connected for this job seeker. "
                    "Open Settings > Mail and connect one."
                )
            self._credentials = _decrypt_credentials(
                self.account.get("credentials_enc"), str(self.job_seeker_id)
            )
        return self._credentials

    def granted_scopes(self) -> set[str]:
        return set(str(self.account.get("scopes") or "").split())

    def access_token(self) -> str:
        """A live access token, refreshing when it is within the skew of expiry."""
        credentials = self.credentials
        expiry = credentials.get("access_token_expires_at")
        fresh = False
        if credentials.get("access_token") and expiry:
            deadline = datetime.fromisoformat(expiry) - timedelta(seconds=EXPIRY_SKEW_SECONDS)
            fresh = datetime.now(UTC) < deadline
        if fresh:
            return str(credentials["access_token"])

        s = get_settings()
        tokens = _token_request(
            {
                "client_id": s.gmail_client_id,
                "client_secret": s.gmail_client_secret,
                "refresh_token": credentials["refresh_token"],
                "grant_type": "refresh_token",
            }
        )
        credentials["access_token"] = tokens.get("access_token", "")
        credentials["access_token_expires_at"] = _expiry_iso(tokens.get("expires_in"))
        # Google may hand back a rotated refresh token; losing it would silently
        # disconnect the mailbox on the next refresh.
        if tokens.get("refresh_token"):
            credentials["refresh_token"] = tokens["refresh_token"]
        if self.account.get("id"):
            repo.set_account_credentials(
                self.account["id"],
                _encrypt_credentials(credentials, str(self.job_seeker_id)),
                token_expires_at=credentials["access_token_expires_at"],
            )
        return str(credentials["access_token"])

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token()}"}

    # -- the IMAP transport's view of this mailbox -------------------------
    # ``mail.inbox.poll_imap`` asks the backend where to connect and how to
    # authenticate rather than branching on the backend's name, so a second
    # IMAP-speaking backend needs no change there at all.
    def imap_endpoint(self) -> tuple[str, int]:
        s = get_settings()
        return s.imap_host, s.imap_port

    def imap_password(self) -> str | None:
        """``None`` means "authenticate with XOAUTH2", which is Gmail's way."""
        return None

    # -- MailBackend ------------------------------------------------------
    def sender_address(self) -> str:
        address = self.account.get("address")
        if not address:
            raise MailBackendNotConfigured("The connected Gmail mailbox has no address recorded.")
        return str(address)

    def status(self) -> dict:
        s = get_settings()
        if not s.gmail_client_id or not s.gmail_client_secret:
            return {
                "backend": self.key,
                "configured": False,
                "reason": "GMAIL_CLIENT_ID / GMAIL_CLIENT_SECRET are not set in .env",
                "capabilities": self.capabilities.__dict__,
            }
        if not self.account or not self.account.get("credentials_enc"):
            return {
                "backend": self.key,
                "configured": False,
                "reason": "No Gmail mailbox connected yet - run the authorisation flow",
                "authorize_path": "/api/mail/gmail/authorize",
                "capabilities": self.capabilities.__dict__,
            }
        return {
            "backend": self.key,
            "configured": True,
            "address": self.account.get("address"),
            "scopes": sorted(self.granted_scopes()),
            "token_expires_at": self.account.get("token_expires_at"),
            "imap_available": IMAP_SCOPE in self.granted_scopes(),
            "capabilities": self.capabilities.__dict__,
        }

    def send(self, message: OutgoingMessage) -> SendResult:
        from dreamjob.mail.composer import to_mime  # noqa: PLC0415 - avoids an import cycle

        self.check_message(message)
        if not message.from_email:
            message.from_email = self.sender_address()
        mime = to_mime(message)
        raw = mime.as_bytes()
        payload = base64.urlsafe_b64encode(raw).decode("ascii")

        try:
            with httpx.Client(timeout=120.0) as client:
                if len(raw) <= SIMPLE_SEND_LIMIT:
                    resp = client.post(
                        f"{API_ROOT}/messages/send",
                        headers=self._headers(),
                        json={"raw": payload},
                    )
                else:
                    resp = client.post(
                        f"{UPLOAD_ROOT}/messages/send",
                        params={"uploadType": "media"},
                        headers={**self._headers(), "Content-Type": "message/rfc822"},
                        content=raw,
                    )
        except httpx.HTTPError as exc:
            raise MailBackendUnavailable(f"Gmail send failed: {exc}") from exc

        if resp.status_code >= 400:
            raise MailBackendUnavailable(
                f"Gmail refused the message ({resp.status_code}): {resp.text[:300]}"
            )

        body = resp.json()
        gmail_id = body.get("id", "")
        # Gmail assigns its own RFC 5322 Message-ID on send.  Reading it back is
        # what keeps FR-326 reply matching working: the id in the send log has
        # to be the one that will come back in the recipient's References.
        sent_message_id = self._sent_message_id(gmail_id) or message.message_id
        return SendResult(
            message_id=sent_message_id,
            thread_id=body.get("threadId"),
            status="sent",
            backend=self.key,
            detail={
                "provider_id": gmail_id,
                "label_ids": body.get("labelIds", []),
                "composed_message_id": message.message_id,
                "bytes": len(raw),
            },
        )

    def _sent_message_id(self, gmail_id: str) -> str | None:
        if not gmail_id:
            return None
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(
                    f"{API_ROOT}/messages/{gmail_id}",
                    headers=self._headers(),
                    params={"format": "metadata", "metadataHeaders": "Message-Id"},
                )
            resp.raise_for_status()
            headers = resp.json().get("payload", {}).get("headers", [])
        except (httpx.HTTPError, ValueError, MailBackendError) as exc:
            log.warning("Could not read back the sent Message-ID from Gmail: %s", exc)
            return None
        for header in headers:
            if str(header.get("name", "")).lower() == "message-id":
                return str(header.get("value", "")).strip()
        return None

    # -- reading, for FR-326 ----------------------------------------------
    def list_message_ids(self, query: str, *, max_results: int = 50) -> list[str]:
        """Gmail search, used by the inbox poller (needs ``gmail.readonly``)."""
        out: list[str] = []
        page_token: str | None = None
        try:
            with httpx.Client(timeout=60.0) as client:
                while len(out) < max_results:
                    params: dict[str, Any] = {
                        "q": query,
                        "maxResults": min(100, max_results - len(out)),
                    }
                    if page_token:
                        params["pageToken"] = page_token
                    resp = client.get(
                        f"{API_ROOT}/messages", headers=self._headers(), params=params
                    )
                    resp.raise_for_status()
                    body = resp.json()
                    out.extend(m["id"] for m in body.get("messages", []) or [])
                    page_token = body.get("nextPageToken")
                    if not page_token:
                        break
        except httpx.HTTPError as exc:
            raise MailBackendUnavailable(f"Gmail search failed: {exc}") from exc
        return out

    def fetch_raw(self, gmail_id: str) -> bytes:
        """The full RFC 822 source, so bounce reports can be parsed structurally."""
        try:
            with httpx.Client(timeout=60.0) as client:
                resp = client.get(
                    f"{API_ROOT}/messages/{gmail_id}",
                    headers=self._headers(),
                    params={"format": "raw"},
                )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise MailBackendUnavailable(f"Gmail fetch failed for {gmail_id}: {exc}") from exc
        raw = resp.json().get("raw", "")
        return base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))


def to_mime_bytes(message: OutgoingMessage) -> bytes:
    """Convenience for tests and for the IMAP APPEND path."""
    from dreamjob.mail.composer import to_mime  # noqa: PLC0415

    mime: EmailMessage = to_mime(message)
    return mime.as_bytes()
