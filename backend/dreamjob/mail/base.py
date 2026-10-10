"""The mail backend interface (FR-325, NFR-204, RK-05).

FR-325 asks that dispatch use *the job seeker's own mailbox*, and section 2.4
states why: replies have to land in their inbox.  Two backends satisfy that to
different degrees, and the product owner wants both, so the difference is made
explicit rather than hidden:

``gmail_oauth``
    The job seeker's own Gmail, connected over OAuth 2.0.  Sends as them,
    threads in their Sent folder, and their inbox receives both the replies and
    the delivery-status notifications - the only backend that meets section 2.4
    in full.

``imap_basic``
    Any mailbox that speaks IMAP and SMTP with an address and an application
    password - Yahoo, iCloud, Outlook, a self-hosted server.  It meets section
    2.4 exactly as ``gmail_oauth`` does (the message leaves the job seeker's
    own mailbox, so replies and delivery-status notifications land in their own
    inbox); the difference is only in how the mailbox is authorised.  It exists
    because OAuth is not on offer everywhere: Yahoo, for one, has no public
    OAuth programme for third-party mail clients, and an application password
    is the credential it issues instead.

``resend``
    A transactional relay on ``stepvda.com``.  It sends when no personal
    mailbox is connected; ``Reply-To`` still points at the job seeker, but the
    delivery news arrives as a webhook rather than in their inbox, because the
    relay has no IMAP.

A backend is chosen per ``mail_account`` row, never per call site, so the
dispatcher's guard rails (FR-325) run identically whichever one is in force.
:class:`BackendCapabilities` is what the rest of the code branches on - the
dispatcher asks "does this backend detect bounces itself?", not "is this
Gmail?".
"""

from __future__ import annotations

import json
import mimetypes
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from dreamjob.config import get_settings


class MailBackendError(RuntimeError):
    """A send failed for a reason the job seeker can be told about."""


class MailBackendNotConfigured(MailBackendError):
    """Credentials are missing.

    Carried separately from :class:`MailBackendError` because it is not a
    failure of the send but of the setup, and the status endpoint reports it
    without anyone having to attempt a send first.
    """


class MailBackendUnavailable(MailBackendError):
    """The provider was reachable but refused or errored; retrying may work."""


# ---------------------------------------------------------------------------
# Credentials at rest (NFR-204)
# ---------------------------------------------------------------------------

#: One crypto purpose for every mailbox credential.  ``crypto.encrypt`` binds
#: both the purpose and the per-seeker scope into the derived key, so a blob
#: written for one job seeker's mailbox cannot be read as another's - and the
#: purpose keeps mailbox credentials separate from profile documents, which use
#: the same master key under a different purpose.
CRYPTO_PURPOSE = "mail"


def encrypt_credentials(credentials: dict, job_seeker_id: str, *, what: str = "mailbox") -> bytes:
    """Seal a credential dict for storage in ``mail_account.credentials_enc``.

    Refusing to store anything when no master key is set is deliberate: NFR-204
    requires mailbox credentials to be encrypted at rest, and a plaintext
    fallback would quietly break that the first time someone ran without a key.
    """
    from dreamjob.security.crypto import CryptoUnavailable, encrypt  # noqa: PLC0415

    try:
        return encrypt(
            json.dumps(credentials).encode("utf-8"), purpose=CRYPTO_PURPOSE, scope=job_seeker_id
        )
    except CryptoUnavailable as exc:
        raise MailBackendError(
            f"Cannot store the {what} credential: DREAMJOB_MASTER_KEY is not set, and NFR-204 "
            "requires mailbox credentials to be encrypted at rest. Generate one with "
            "`python -m dreamjob.security.crypto --generate-key` and restart."
        ) from exc


def decrypt_credentials(
    blob: bytes | None,
    job_seeker_id: str,
    *,
    what: str = "mailbox",
    reconnect_hint: str = "Open Settings > Mail and connect it again.",
) -> dict:
    """Open a stored credential.  Raises with something the job seeker can act on.

    A wrong master key and a corrupted blob are indistinguishable from here -
    both are an AES-GCM tag failure - so they share one message that names the
    likely cause and the fix.
    """
    from dreamjob.security.crypto import CryptoUnavailable, decrypt  # noqa: PLC0415

    if not blob:
        raise MailBackendNotConfigured(f"This {what} is not connected. {reconnect_hint}")
    try:
        return json.loads(decrypt(bytes(blob), purpose=CRYPTO_PURPOSE, scope=job_seeker_id))
    except CryptoUnavailable as exc:
        raise MailBackendError(
            f"DREAMJOB_MASTER_KEY is not set, so the stored {what} credential cannot be decrypted."
        ) from exc
    except Exception as exc:  # noqa: BLE001 - a wrong key looks exactly like corruption
        raise MailBackendError(
            f"The stored {what} credential could not be decrypted (wrong master key, or it was "
            f"written under a different one). {reconnect_hint}"
        ) from exc


@dataclass(frozen=True)
class Attachment:
    """One file on the message.  Held in memory: CVs are a few hundred kB."""

    filename: str
    content: bytes
    content_type: str = "application/octet-stream"
    source_path: str | None = None

    @classmethod
    def from_path(cls, path: str | Path, *, filename: str | None = None) -> Attachment:
        p = Path(path)
        guessed, _ = mimetypes.guess_type(p.name)
        return cls(
            filename=filename or p.name,
            content=p.read_bytes(),
            content_type=guessed or "application/octet-stream",
            source_path=str(p),
        )

    @property
    def maintype(self) -> str:
        return self.content_type.split("/", 1)[0]

    @property
    def subtype(self) -> str:
        parts = self.content_type.split("/", 1)
        return parts[1] if len(parts) == 2 else "octet-stream"


@dataclass
class OutgoingMessage:
    """A composed, ready-to-send message.

    ``message_id`` is generated by the composer rather than by the provider so
    that the send log holds it before the send happens; that is what lets a
    reply be matched back to a dispatch even if the send itself times out
    (FR-326).
    """

    to_email: str
    subject: str
    body_text: str
    to_name: str | None = None
    from_email: str = ""
    from_name: str | None = None
    reply_to: str | None = None
    message_id: str = ""
    in_reply_to: str | None = None
    references: str | None = None
    language: str = "en"
    attachments: list[Attachment] = field(default_factory=list)
    extra_headers: dict[str, str] = field(default_factory=dict)

    @property
    def attachment_names(self) -> list[str]:
        return [a.filename for a in self.attachments]

    @property
    def attachment_paths(self) -> list[str]:
        return [a.source_path for a in self.attachments if a.source_path]


@dataclass
class SendResult:
    """What the provider said.  ``message_id`` is the RFC 5322 id we threaded on."""

    message_id: str
    thread_id: str | None
    status: str  # sent | failed
    backend: str = ""
    detail: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "sent"


@dataclass(frozen=True)
class BackendCapabilities:
    """What a backend can do, so callers branch on behaviour, not on its name."""

    key: str
    display_name: str
    can_send: bool = True
    #: Replies arrive in the job seeker's own mailbox (section 2.4, RK-05).
    replies_to_seeker_inbox: bool = False
    #: "imap" | "gmail_api" | "webhook" | "none" - how FR-326 learns of bounces.
    bounce_detection: str = "none"
    #: Reply detection is a poll (we ask) or a push (they tell us).
    reply_detection: str = "none"
    requires_oauth: bool = False
    supports_attachments: bool = True
    #: Largest total message size the provider accepts, in bytes.
    max_message_bytes: int = 25 * 1024 * 1024


class MailBackend(ABC):
    """One way of putting a message on the wire.

    Implementations are constructed with the ``mail_account`` row they act for
    (``None`` for a relay that has no per-seeker account), and must not reach
    into the database themselves: repository access belongs to the dispatcher,
    which owns the transaction and the audit trail (NFR-702).
    """

    capabilities: ClassVar[BackendCapabilities]

    def __init__(self, account: dict | None = None, *, job_seeker_id: str | None = None):
        self.account = account or {}
        self.job_seeker_id = job_seeker_id or self.account.get("job_seeker_id")
        self.settings = get_settings()

    @property
    def key(self) -> str:
        return self.capabilities.key

    @abstractmethod
    def send(self, message: OutgoingMessage) -> SendResult:
        """Send one message.

        Raises :class:`MailBackendNotConfigured` when credentials are missing
        and :class:`MailBackendUnavailable` when the provider rejected the
        request; both messages are written to be shown to the job seeker.
        """

    @abstractmethod
    def status(self) -> dict:
        """Configuration state, for the mail status screen.

        Always answers ``{"backend", "configured", "reason", "capabilities"}``
        without attempting a send, so an unconfigured backend is reported as
        such rather than discovered at dispatch time.
        """

    def sender_address(self) -> str:
        raise MailBackendNotConfigured(f"{self.key}: no sender address configured")

    def check_message(self, message: OutgoingMessage) -> None:
        """Provider-independent preconditions, checked before every send."""
        if not message.to_email or "@" not in message.to_email:
            raise MailBackendError(f"{self.key}: no valid recipient address on the message")
        if not message.subject.strip():
            raise MailBackendError(f"{self.key}: refusing to send a message with no subject")
        size = sum(len(a.content) for a in message.attachments) + len(message.body_text.encode())
        if size > self.capabilities.max_message_bytes:
            raise MailBackendError(
                f"{self.key}: message is {size // 1024} kB, over the "
                f"{self.capabilities.max_message_bytes // 1024} kB provider limit; "
                "attach a lighter CV export"
            )


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, type[MailBackend]] = {}
#: Whether the concrete backends have been imported.  A separate flag rather
#: than "is the registry empty?": importing one backend module directly - which
#: a caller or a test may do - registers that one and would otherwise convince
#: the loader its work was done, leaving the others permanently invisible.
_LOADED = False


def register_backend(cls: type[MailBackend]) -> type[MailBackend]:
    _REGISTRY[cls.capabilities.key] = cls
    return cls


def backend_class(key: str) -> type[MailBackend]:
    """The backend registered under ``key``.

    Tolerates a key registered after the first load (the test fakes do this) by
    looking before and after, so a late registration is not reported as unknown.
    """
    if key not in _REGISTRY:
        _load_backends()
    try:
        return _REGISTRY[key]
    except KeyError:
        raise MailBackendError(
            f"Unknown mail backend {key!r}; known backends: {', '.join(sorted(_REGISTRY))}"
        ) from None


def get_backend(key: str, account: dict | None = None, **kwargs: object) -> MailBackend:
    return backend_class(key)(account, **kwargs)  # type: ignore[arg-type]


def backend_for_account(account: dict) -> MailBackend:
    """FR-325: the backend follows the mailbox, not the call site."""
    return get_backend(account["backend"], account)


def pollable_backends() -> list[str]:
    """Backend keys whose mailbox the inbox poller can read (FR-326).

    Derived from the capability rather than listed by hand, so adding a backend
    that polls does not mean remembering to edit the poller and the repository
    query as well.
    """
    return [c.key for c in all_capabilities() if c.reply_detection == "poll"]


def all_capabilities() -> list[BackendCapabilities]:
    _load_backends()
    return [cls.capabilities for cls in _REGISTRY.values()]


def _load_backends() -> None:
    """Import the concrete backends once, on first use (avoids an import cycle)."""
    global _LOADED
    if _LOADED:
        return
    # Set first: each import below triggers register_backend, which must not
    # recurse back into here.
    _LOADED = True
    from dreamjob.mail import gmail, imap_basic, resend_backend  # noqa: F401,PLC0415
