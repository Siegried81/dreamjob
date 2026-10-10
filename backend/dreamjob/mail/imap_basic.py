"""Any IMAP + SMTP mailbox, authorised with an application password (FR-325, FR-326, NFR-204).

Why this exists beside ``gmail_oauth``
-------------------------------------
``gmail_oauth`` is the better credential - revocable at the provider, scoped,
no password at rest - but it is Google-only.  Yahoo, iCloud and most self-hosted
servers run no OAuth programme a third-party mail client can join, and the
credential they issue instead is an *application password*: a provider-generated
secret, distinct from the account password, revocable from the provider's own
security page, and useless for signing in to the web account.

That is the credential this backend stores, and it buys the same section 2.4
property ``gmail_oauth`` has: the message leaves the job seeker's own mailbox,
so replies and delivery-status notifications arrive in their own inbox where
FR-326 can read them.

What is stored, and where
-------------------------
Everything needed to reconnect goes in one encrypted blob in
``mail_account.credentials_enc`` - the password together with the host, port
and folder.  The hosts are not secret, but keeping them in the same envelope
means one mailbox is one row with no schema change, and a job seeker with two
mailboxes on different providers is just two rows.  The envelope itself is
``base.encrypt_credentials``, so the password is encrypted per job seeker
(NFR-204) exactly as the Gmail refresh token is.

The credential is verified against the provider *before* it is stored
(:func:`connect`), because a password that only fails at the next poll is a
silent mailbox: the poller swallows per-account errors so one bad mailbox
cannot stop the others, which is right for the job and wrong for setup.

Note on reading (FR-326)
------------------------
The poller reads this mailbox through :func:`dreamjob.mail.inbox.poll_imap`,
unchanged: that function asks the backend for its endpoint and its password, so
the only difference from Gmail-over-IMAP is that :meth:`imap_password` answers
with a password instead of ``None``.  Reading is also all it does - a message
that matches no dispatch Dream Job sent is recorded and classified, never
turned into an opportunity, and nothing here ever deletes or moves mail.  The
IMAP session is opened ``readonly``.
"""

from __future__ import annotations

import imaplib
import logging
import smtplib
import ssl
from typing import ClassVar

from dreamjob.config import get_settings
from dreamjob.db.repositories import dispatch as repo
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

BACKEND_KEY = "imap_basic"

#: How long to wait on a provider socket.  Deliberately short for the login
#: probe in :func:`connect` (someone is watching a form) and longer for a send,
#: which carries a CV attachment.
PROBE_TIMEOUT = 20.0
SEND_TIMEOUT = 120.0


@register_backend
class ImapBasicBackend(MailBackend):
    """Send over SMTP, read over IMAP, authorised by an application password."""

    capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
        key=BACKEND_KEY,
        display_name="IMAP / SMTP (app password)",
        # Same section 2.4 standing as Gmail: it is the job seeker's own mailbox.
        replies_to_seeker_inbox=True,
        bounce_detection="imap",
        reply_detection="poll",
        requires_oauth=False,
        # The lowest common ceiling among the providers this targets; Yahoo and
        # iCloud both stop at 25 MB, so a larger message would be accepted here
        # and bounced there, which FR-326 would then have to explain.
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
                    "No IMAP mailbox is connected for this job seeker. "
                    "Open Settings > Mail and connect one."
                )
            self._credentials = decrypt_credentials(
                self.account.get("credentials_enc"),
                str(self.job_seeker_id),
                what="IMAP mailbox",
                reconnect_hint="Open Settings > Mail and reconnect it with a fresh app password.",
            )
        return self._credentials

    # -- the IMAP transport's view of this mailbox (see mail.inbox) --------
    def imap_endpoint(self) -> tuple[str, int]:
        c = self.credentials
        return str(c["imap_host"]), int(c["imap_port"])

    def imap_password(self) -> str | None:
        return str(self.credentials["password"])

    def imap_folder(self) -> str:
        return str(self.credentials.get("folder") or "INBOX")

    # -- MailBackend ------------------------------------------------------
    def sender_address(self) -> str:
        address = self.account.get("address")
        if not address:
            raise MailBackendNotConfigured("The connected IMAP mailbox has no address recorded.")
        return str(address)

    def status(self) -> dict:
        if not self.account or not self.account.get("credentials_enc"):
            return {
                "backend": self.key,
                "configured": False,
                "reason": (
                    "No IMAP mailbox connected yet - connect one with an app password "
                    "from your provider"
                ),
                "connect_path": "/api/mail/imap/connect",
                "providers": sorted(PROVIDERS),
                "capabilities": self.capabilities.__dict__,
            }
        # Read the envelope so a mailbox whose blob cannot be opened (master key
        # changed) reports that here rather than at the next send.
        try:
            c = self.credentials
        except MailBackendError as exc:
            return {
                "backend": self.key,
                "configured": False,
                "reason": str(exc),
                "capabilities": self.capabilities.__dict__,
            }
        return {
            "backend": self.key,
            "configured": True,
            "address": self.account.get("address"),
            "imap_host": c.get("imap_host"),
            "smtp_host": c.get("smtp_host"),
            "folder": c.get("folder", "INBOX"),
            "provider": c.get("provider"),
            "capabilities": self.capabilities.__dict__,
        }

    def send(self, message: OutgoingMessage) -> SendResult:
        """Hand the composed MIME to the provider's SMTP, as the job seeker.

        The composer's ``Message-ID`` is the one that goes on the wire and the
        one returned, which is simpler than the Gmail path: SMTP does not
        rewrite it, so the id already in the send log is the id that will come
        back in a reply's ``References`` (FR-326).
        """
        from dreamjob.mail.composer import to_mime  # noqa: PLC0415 - avoids an import cycle

        self.check_message(message)
        address = self.sender_address()
        if not message.from_email:
            message.from_email = address

        c = self.credentials
        mime = to_mime(message)
        raw = mime.as_bytes()
        try:
            with _smtp_client(
                str(c["smtp_host"]), int(c["smtp_port"]), timeout=SEND_TIMEOUT
            ) as client:
                client.login(address, str(c["password"]))
                client.send_message(mime)
        except smtplib.SMTPAuthenticationError as exc:
            raise MailBackendNotConfigured(
                f"{c['smtp_host']} refused the app password for {address}. Generate a new "
                "application password with your provider and reconnect the mailbox."
            ) from exc
        except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
            raise MailBackendUnavailable(
                f"SMTP send through {c['smtp_host']} failed: {exc}"
            ) from exc

        return SendResult(
            message_id=message.message_id,
            thread_id=None,
            status="sent",
            backend=self.key,
            detail={
                "smtp_host": c["smtp_host"],
                "composed_message_id": message.message_id,
                "bytes": len(raw),
            },
        )


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

#: Hosts and ports per provider, so connecting is "pick Yahoo and paste the app
#: password" rather than four fields nobody remembers.  Both ports are the
#: implicit-TLS ones (SMTPS 465, IMAPS 993) because every provider here offers
#: them and they need no STARTTLS upgrade step that could silently not happen.
PROVIDERS: dict[str, dict[str, object]] = {
    "yahoo": {
        "label": "Yahoo Mail",
        "imap_host": "imap.mail.yahoo.com",
        "smtp_host": "smtp.mail.yahoo.com",
        # Yahoo issues these at
        # https://login.yahoo.com/account/security -> "Generate app password".
        "app_password_url": "https://login.yahoo.com/account/security",
    },
    "icloud": {
        "label": "iCloud Mail",
        "imap_host": "imap.mail.me.com",
        "smtp_host": "smtp.mail.me.com",
        "app_password_url": "https://account.apple.com/account/manage",
    },
    "outlook": {
        "label": "Outlook / Hotmail",
        "imap_host": "outlook.office365.com",
        "smtp_host": "smtp-mail.outlook.com",
        "app_password_url": "https://account.microsoft.com/security",
    },
    "gmail": {
        # Offered for completeness, but gmail_oauth is the better credential and
        # the status screen says so: an app password here is a password at rest.
        "label": "Gmail (app password - prefer the OAuth backend)",
        "imap_host": "imap.gmail.com",
        "smtp_host": "smtp.gmail.com",
        "app_password_url": "https://myaccount.google.com/apppasswords",
    },
}

DEFAULT_IMAP_PORT = 993
DEFAULT_SMTP_PORT = 465


def provider_choices() -> list[dict]:
    """What the connect screen offers, including where to get the password."""
    return [{"key": key, **{k: v for k, v in spec.items()}} for key, spec in PROVIDERS.items()]


def resolve_provider(
    provider: str | None,
    *,
    imap_host: str | None = None,
    smtp_host: str | None = None,
    imap_port: int | None = None,
    smtp_port: int | None = None,
) -> dict:
    """Turn a provider key, or explicit hosts, into one settings dict.

    Explicit hosts win over the preset, so a self-hosted server needs no entry
    in :data:`PROVIDERS` - which is the point of keeping the presets a
    convenience rather than an allowlist.
    """
    preset: dict[str, object] = {}
    key = (provider or "").strip().lower()
    if key:
        if key not in PROVIDERS:
            raise MailBackendError(
                f"Unknown mail provider {provider!r}; known: {', '.join(sorted(PROVIDERS))}. "
                "Leave it empty and give imap_host / smtp_host directly for anything else."
            )
        preset = PROVIDERS[key]

    resolved_imap = (imap_host or preset.get("imap_host") or "").strip()
    resolved_smtp = (smtp_host or preset.get("smtp_host") or "").strip()
    if not resolved_imap or not resolved_smtp:
        raise MailBackendError(
            "An IMAP host and an SMTP host are needed. Either name a provider "
            f"({', '.join(sorted(PROVIDERS))}) or give imap_host and smtp_host."
        )
    return {
        "provider": key or "custom",
        "imap_host": resolved_imap,
        "imap_port": int(imap_port or DEFAULT_IMAP_PORT),
        "smtp_host": resolved_smtp,
        "smtp_port": int(smtp_port or DEFAULT_SMTP_PORT),
    }


# ---------------------------------------------------------------------------
# Connecting a mailbox
# ---------------------------------------------------------------------------


def _imap_client(host: str, port: int, *, timeout: float) -> imaplib.IMAP4_SSL:
    """Indirected so tests can substitute a fake server without a socket."""
    return imaplib.IMAP4_SSL(host, port, timeout=timeout)


def _smtp_client(host: str, port: int, *, timeout: float) -> smtplib.SMTP_SSL:
    return smtplib.SMTP_SSL(host, port, timeout=timeout)


def verify_credentials(
    address: str, password: str, settings: dict, *, folder: str = "INBOX"
) -> dict:
    """Log in to both halves before anything is stored.

    IMAP and SMTP are checked separately because providers fail them
    separately: Yahoo, for instance, accepts an app password on IMAP while
    refusing an account that has not enabled SMTP access, and reporting "the
    password is wrong" for that would send someone to regenerate a password
    that was fine.
    """
    checked: dict[str, object] = {}

    try:
        client = _imap_client(
            str(settings["imap_host"]), int(settings["imap_port"]), timeout=PROBE_TIMEOUT
        )
    except (OSError, ssl.SSLError, imaplib.IMAP4.error) as exc:
        raise MailBackendUnavailable(
            f"Could not reach {settings['imap_host']}:{settings['imap_port']}: {exc}"
        ) from exc
    try:
        client.login(address, password)
        # Selecting proves the folder exists, which is the other way setup goes
        # wrong: a folder named in the UI that IMAP spells differently.
        typ, _ = client.select(folder, readonly=True)
        if typ != "OK":
            raise MailBackendError(
                f"Signed in to {settings['imap_host']}, but the folder {folder!r} does not exist "
                "on this mailbox. Check its exact name in your webmail."
            )
        checked["imap"] = "ok"
    except imaplib.IMAP4.error as exc:
        raise MailBackendNotConfigured(
            f"{settings['imap_host']} refused the app password for {address} ({exc}). Note that "
            "providers want an *application* password here, not the one you use on their website."
        ) from exc
    finally:
        try:
            client.logout()
        except (OSError, imaplib.IMAP4.error):  # pragma: no cover - best effort
            pass

    try:
        with _smtp_client(
            str(settings["smtp_host"]), int(settings["smtp_port"]), timeout=PROBE_TIMEOUT
        ) as smtp:
            smtp.login(address, password)
            checked["smtp"] = "ok"
    except smtplib.SMTPAuthenticationError as exc:
        raise MailBackendNotConfigured(
            f"IMAP accepted the password but {settings['smtp_host']} refused it ({exc}). Check "
            "that outgoing (SMTP) access is enabled for this mailbox."
        ) from exc
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        raise MailBackendUnavailable(
            f"Could not reach {settings['smtp_host']}:{settings['smtp_port']}: {exc}"
        ) from exc

    return checked


def connect(
    job_seeker_id: str,
    *,
    address: str,
    password: str,
    provider: str | None = None,
    imap_host: str | None = None,
    imap_port: int | None = None,
    smtp_host: str | None = None,
    smtp_port: int | None = None,
    folder: str = "INBOX",
    verify: bool = True,
) -> dict:
    """Verify an app password, then store the mailbox.  Returns the account row.

    ``verify=False`` exists for the tests and for a deliberate offline setup; it
    is not exposed on the API, because storing a credential nobody has checked
    is how a mailbox ends up silently not polling.
    """
    address = address.strip().lower()
    if not address or "@" not in address:
        raise MailBackendError(f"{address!r} is not an e-mail address.")
    if not password.strip():
        raise MailBackendError("An application password is required.")

    settings = resolve_provider(
        provider,
        imap_host=imap_host,
        smtp_host=smtp_host,
        imap_port=imap_port,
        smtp_port=smtp_port,
    )
    folder = folder.strip() or "INBOX"
    checked = verify_credentials(address, password, settings, folder=folder) if verify else {}

    credentials = {**settings, "password": password, "folder": folder}
    account_id = repo.upsert_account(
        job_seeker_id,
        backend=BACKEND_KEY,
        address=address,
        credentials_enc=encrypt_credentials(credentials, job_seeker_id, what="IMAP mailbox"),
        # No OAuth scopes exist for this backend.  The column is left null
        # rather than filled with something scope-shaped, so that the IMAP-scope
        # test in mail.inbox cannot accidentally match on it.
        scopes=None,
    )
    # Seed the poll cursor with the folder, so the first poll reads the right
    # one without the caller having to know that poll state carries it.
    repo.save_poll_state(account_id, {"folder": folder})

    log.info(
        "Connected IMAP mailbox %s (%s) for seeker %s", address, settings["provider"], job_seeker_id
    )
    return {
        "id": account_id,
        "job_seeker_id": job_seeker_id,
        "backend": BACKEND_KEY,
        "address": address,
        "folder": folder,
        "verified": checked,
        **{k: v for k, v in settings.items() if k != "password"},
    }


def disconnect(account: dict) -> dict:
    """Forget the password.

    There is no provider-side revocation to call: an application password is
    revoked by the job seeker on the provider's own security page, and the
    result says so, so the UI can tell them that deleting it here is only half
    the job.
    """
    repo.clear_credentials(account["id"])
    provider = ""
    try:
        provider = str(
            decrypt_credentials(
                account.get("credentials_enc"), str(account["job_seeker_id"]), what="IMAP mailbox"
            ).get("provider", "")
        )
    except MailBackendError:  # pragma: no cover - the blob may already be gone
        pass
    url = str(PROVIDERS.get(provider, {}).get("app_password_url", "")) if provider else ""
    return {
        "revoked_locally": True,
        "revoked_at_provider": False,
        "provider": provider,
        "revoke_hint": (
            f"Dream Job has forgotten this password. Revoke it at the provider too: {url}"
            if url
            else "Dream Job has forgotten this password. Revoke it on your provider's "
            "security page as well."
        ),
    }


def sending_is_armed() -> bool:
    """Whether a send would actually leave the machine (RK-05).

    Exposed for the connect screen: a job seeker who has just connected their
    own mailbox should be told plainly that dry-run is still on, rather than
    concluding from a successful connection that mail is now going out.
    """
    return not get_settings().mail_dry_run
