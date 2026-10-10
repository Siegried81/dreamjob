"""The app-password IMAP/SMTP backend (FR-325, FR-326, NFR-204).

Everything here runs against a throwaway SQLite file with no network: the IMAP
and SMTP servers are fakes substituted at the two seams the backend leaves for
exactly that (``imap_basic._imap_client`` / ``_smtp_client``, and
``inbox.imaplib``), so the login probe, the UID cursor and the credential
envelope are exercised for real while nothing opens a socket.

What these tests are pinning down, beyond "it works":

* the credential is encrypted at rest, and the plaintext password appears
  nowhere in the row (NFR-204);
* a mailbox is not stored when the provider refused it, because a credential
  that only fails at the next poll makes a mailbox that looks connected and
  reads nothing;
* the poller is driven by the backend's *capabilities*, so an app-password
  mailbox is polled and the webhook relay is skipped with a reason - this is
  the gate that previously read ``backend != "gmail_oauth"`` and silently
  dropped every other mailbox;
* reading never writes: the IMAP session is opened read-only and a message
  matching no dispatch becomes an ``incoming_reply``, never an opportunity.
"""

from __future__ import annotations

import base64
import email.utils
import os
import secrets
from collections.abc import Iterator
from pathlib import Path

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "DREAMJOB_MAIL_DRY_RUN",
)

ADDRESS = "seeker@yahoo.fr"
APP_PASSWORD = "abcdefghijklmnop"  # the 16-character shape Yahoo issues


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    # Pinned to the shipped default (RK-05): dry_run.install_transport_guard()
    # wraps every backend in dreamjob.mail for the whole process, so whether the
    # guard fires here must not depend on which test file ran first.
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true"
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


@pytest.fixture
def seeker_id() -> str:
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    return insert_row(
        "job_seeker",
        {
            "email": ADDRESS,
            "display_name": "Stephane van der Aa",
            "locale": "fr",
            "created_at": now,
            "updated_at": now,
        },
    )


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _raw_message(
    *, subject: str, from_address: str, to: str = ADDRESS, message_id: str = "", body: str = "hello"
) -> bytes:
    """One RFC 5322 message as a server would hand it back."""
    mid = message_id or email.utils.make_msgid(domain="example.test")
    return (
        f"From: {from_address}\r\n"
        f"To: {to}\r\n"
        f"Subject: {subject}\r\n"
        f"Message-ID: {mid}\r\n"
        f"Date: {email.utils.formatdate()}\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        f"{body}\r\n"
    ).encode()


class FakeIMAP:
    """Just enough IMAP4_SSL for the poller: login, select, status, uid, logout.

    Records what it was asked so the tests can assert on the two things that
    matter and are invisible in the return value: that the session was opened
    read-only, and that the folder selected was the one the mailbox was
    connected with.
    """

    #: Set per test before the class is used as a constructor.
    password: str | None = APP_PASSWORD
    messages: dict[int, bytes] = {}
    uid_validity: str = "1000"
    folders: tuple[str, ...] = ("INBOX",)

    instances: list[FakeIMAP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.logged_in_as: str | None = None
        self.selected: str | None = None
        self.readonly: bool | None = None
        self.xoauth2_used = False
        self.logged_out = False
        self.fetched: list[int] = []
        FakeIMAP.instances.append(self)

    # -- auth -------------------------------------------------------------
    def login(self, user: str, password: str):
        import imaplib

        if FakeIMAP.password is None or password != FakeIMAP.password:
            raise imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")
        self.logged_in_as = user
        return ("OK", [b"LOGIN completed"])

    def authenticate(self, mechanism: str, authobject):
        self.xoauth2_used = True
        authobject(b"")
        return ("OK", [b"AUTH completed"])

    # -- folders ----------------------------------------------------------
    def select(self, folder: str = "INBOX", readonly: bool = False):
        if folder not in FakeIMAP.folders:
            return ("NO", [b"no such mailbox"])
        self.selected = folder
        self.readonly = readonly
        return ("OK", [str(len(FakeIMAP.messages)).encode()])

    def status(self, folder: str, what: str):
        return ("OK", [f'"{folder}" (UIDVALIDITY {FakeIMAP.uid_validity})'.encode()])

    # -- reading ----------------------------------------------------------
    def uid(self, command: str, *args):
        if command.upper() == "SEARCH":
            # args is (None, "UID <low>:*") as imaplib's signature wants.
            spec = args[-1]
            low = int(str(spec).split()[1].split(":")[0])
            hits = sorted(u for u in FakeIMAP.messages if u >= low)
            return ("OK", [" ".join(str(u) for u in hits).encode()])
        if command.upper() == "FETCH":
            uid = int(args[0])
            self.fetched.append(uid)
            raw = FakeIMAP.messages[uid]
            return ("OK", [(f"{uid} (RFC822 {{{len(raw)}}}".encode(), raw), b")"])
        raise AssertionError(f"unexpected IMAP command {command}")

    def logout(self):
        self.logged_out = True
        return ("BYE", [b"logging out"])


class FakeSMTP:
    """Context-managed stand-in for smtplib.SMTP_SSL."""

    password: str | None = APP_PASSWORD
    sent: list = []
    instances: list[FakeSMTP] = []

    def __init__(self, host: str, port: int, timeout: float | None = None):
        self.host = host
        self.port = port
        self.logged_in_as: str | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user: str, password: str):
        import smtplib

        if FakeSMTP.password is None or password != FakeSMTP.password:
            raise smtplib.SMTPAuthenticationError(535, b"Invalid credentials")
        self.logged_in_as = user

    def send_message(self, mime):
        FakeSMTP.sent.append(mime)


@pytest.fixture(autouse=True)
def fakes(monkeypatch) -> Iterator[None]:
    """Substitute both servers, and reset the class-level state between tests."""
    from dreamjob.mail import imap_basic

    FakeIMAP.instances = []
    FakeIMAP.password = APP_PASSWORD
    FakeIMAP.uid_validity = "1000"
    FakeIMAP.folders = ("INBOX",)
    FakeIMAP.messages = {}
    FakeSMTP.instances = []
    FakeSMTP.password = APP_PASSWORD
    FakeSMTP.sent = []

    monkeypatch.setattr(
        imap_basic, "_imap_client", lambda host, port, *, timeout: FakeIMAP(host, port, timeout)
    )
    monkeypatch.setattr(
        imap_basic, "_smtp_client", lambda host, port, *, timeout: FakeSMTP(host, port, timeout)
    )
    # The poller constructs imaplib.IMAP4_SSL directly; swap the symbol it holds.
    from dreamjob.mail import inbox

    monkeypatch.setattr(inbox.imaplib, "IMAP4_SSL", FakeIMAP)
    yield


# ---------------------------------------------------------------------------
# Provider presets
# ---------------------------------------------------------------------------


def test_yahoo_preset_fills_both_hosts():
    from dreamjob.mail.imap_basic import resolve_provider

    settings = resolve_provider("yahoo")
    assert settings["imap_host"] == "imap.mail.yahoo.com"
    assert settings["smtp_host"] == "smtp.mail.yahoo.com"
    # Implicit TLS on both, so no STARTTLS upgrade can silently not happen.
    assert (settings["imap_port"], settings["smtp_port"]) == (993, 465)


def test_explicit_hosts_win_over_the_preset():
    """A self-hosted server needs no entry in PROVIDERS."""
    from dreamjob.mail.imap_basic import resolve_provider

    settings = resolve_provider(None, imap_host="mail.example.test", smtp_host="mail.example.test")
    assert settings["provider"] == "custom"
    assert settings["imap_host"] == "mail.example.test"


def test_unknown_provider_is_refused_by_name():
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import resolve_provider

    with pytest.raises(MailBackendError, match="Unknown mail provider"):
        resolve_provider("yahou")


def test_a_provider_with_no_hosts_at_all_is_refused():
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import resolve_provider

    with pytest.raises(MailBackendError, match="IMAP host and an SMTP host"):
        resolve_provider(None)


# ---------------------------------------------------------------------------
# Connecting (NFR-204)
# ---------------------------------------------------------------------------


def test_connect_verifies_both_halves_then_stores(seeker_id: str):
    from dreamjob.mail.imap_basic import connect

    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")

    assert account["backend"] == "imap_basic"
    assert account["address"] == ADDRESS
    assert account["verified"] == {"imap": "ok", "smtp": "ok"}
    # Both halves were actually probed - providers refuse them separately.
    assert FakeIMAP.instances[0].logged_in_as == ADDRESS
    assert FakeSMTP.instances[0].logged_in_as == ADDRESS
    # The probe does not hold the session open.
    assert FakeIMAP.instances[0].logged_out is True
    # And it looks, never touches.
    assert FakeIMAP.instances[0].readonly is True


def test_the_password_is_encrypted_at_rest_and_not_in_the_row(seeker_id: str):
    """NFR-204: the credential exists only inside the envelope."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import connect

    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    row = repo.get_account_any(account["id"])

    blob = bytes(row["credentials_enc"])
    assert APP_PASSWORD.encode() not in blob
    assert APP_PASSWORD not in repr(dict(row))
    # No OAuth scopes are invented for a backend that has none; the IMAP-scope
    # test in the poller must not be able to match on this column.
    assert not row["scopes"]

    # It round-trips through the backend, and only there.
    from dreamjob.mail.base import get_backend

    backend = get_backend("imap_basic", row)
    assert backend.imap_password() == APP_PASSWORD
    assert backend.imap_endpoint() == ("imap.mail.yahoo.com", 993)


def test_a_refused_password_stores_nothing(seeker_id: str):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.base import MailBackendNotConfigured
    from dreamjob.mail.imap_basic import connect

    FakeIMAP.password = "something-else"
    with pytest.raises(MailBackendNotConfigured, match="application. password"):
        connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")

    assert repo.accounts_to_poll(["imap_basic"]) == []


def test_imap_accepting_while_smtp_refuses_says_which_half(seeker_id: str):
    """Reporting "wrong password" here would send someone to regenerate a good one."""
    from dreamjob.mail.base import MailBackendNotConfigured
    from dreamjob.mail.imap_basic import connect

    FakeSMTP.password = "something-else"
    with pytest.raises(MailBackendNotConfigured, match="IMAP accepted the password but"):
        connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")


def test_a_folder_that_does_not_exist_is_caught_at_connect(seeker_id: str):
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    with pytest.raises(MailBackendError, match="does not exist"):
        connect(
            seeker_id,
            address=ADDRESS,
            password=APP_PASSWORD,
            provider="yahoo",
            folder="lkdn-glassdoor",
        )


def test_connect_rejects_a_non_address(seeker_id: str):
    from dreamjob.mail.base import MailBackendError
    from dreamjob.mail.imap_basic import connect

    with pytest.raises(MailBackendError, match="not an e-mail address"):
        connect(seeker_id, address="seeker", password=APP_PASSWORD, provider="yahoo")


def test_reconnecting_replaces_the_credential_rather_than_adding_a_row(seeker_id: str):
    """UNIQUE (job_seeker_id, backend, address): a stale password must not linger."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import connect

    first = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    FakeIMAP.password = FakeSMTP.password = "rotated-password"
    second = connect(seeker_id, address=ADDRESS, password="rotated-password", provider="yahoo")

    assert first["id"] == second["id"]
    assert len(repo.accounts_to_poll(["imap_basic"])) == 1
    from dreamjob.mail.base import get_backend

    row = repo.get_account_any(second["id"])
    assert get_backend("imap_basic", row).imap_password() == "rotated-password"


# ---------------------------------------------------------------------------
# Polling (FR-326) - the gate this change lifted
# ---------------------------------------------------------------------------


@pytest.fixture
def connected(seeker_id: str) -> dict:
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import connect

    account = connect(seeker_id, address=ADDRESS, password=APP_PASSWORD, provider="yahoo")
    return repo.get_account_any(account["id"])


def test_poll_all_now_reads_an_app_password_mailbox(connected: dict, seeker_id: str):
    """The previous gate was ``backend != "gmail_oauth"``; this is the regression."""
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        11: _raw_message(subject="Re: your application", from_address="recruiter@acme.test"),
    }
    results = inbox.poll_all(seeker_id)

    assert len(results) == 1
    assert results[0]["transport"] == "imap"
    assert results[0]["polled"] == 1
    assert "skipped" not in results[0]


def test_polling_uses_the_password_not_xoauth2(connected: dict):
    from dreamjob.mail import inbox

    FakeIMAP.messages = {7: _raw_message(subject="hi", from_address="recruiter@acme.test")}
    inbox.poll_account(connected)

    client = FakeIMAP.instances[-1]
    assert client.logged_in_as == ADDRESS
    assert client.xoauth2_used is False
    # Per-account endpoint, not the global Gmail default.
    assert (client.host, client.port) == ("imap.mail.yahoo.com", 993)


def test_polling_opens_the_mailbox_read_only(connected: dict):
    """Nothing in Dream Job deletes or moves mail; the session cannot."""
    from dreamjob.mail import inbox

    FakeIMAP.messages = {7: _raw_message(subject="hi", from_address="recruiter@acme.test")}
    inbox.poll_account(connected)

    assert FakeIMAP.instances[-1].readonly is True


def test_the_cursor_advances_and_does_not_re_read(connected: dict):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        4: _raw_message(subject="first", from_address="a@acme.test"),
        5: _raw_message(subject="second", from_address="b@acme.test"),
    }
    first = inbox.poll_account(connected)
    assert first["polled"] == 2
    assert repo.get_poll_state(connected["id"])["last_uid"] == 5

    # A second pass with nothing new reads nothing.
    second = inbox.poll_account(connected)
    assert second["polled"] == 0

    FakeIMAP.messages[6] = _raw_message(subject="third", from_address="c@acme.test")
    third = inbox.poll_account(connected)
    assert third["polled"] == 1
    assert FakeIMAP.instances[-1].fetched == [6]


def test_a_changed_uidvalidity_restarts_the_cursor(connected: dict):
    """Stored UIDs mean nothing after the server reissues them."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.messages = {4: _raw_message(subject="first", from_address="a@acme.test")}
    inbox.poll_account(connected)
    assert repo.get_poll_state(connected["id"])["last_uid"] == 4

    FakeIMAP.uid_validity = "2000"
    FakeIMAP.messages = {1: _raw_message(subject="renumbered", from_address="d@acme.test")}
    result = inbox.poll_account(connected)

    assert result["polled"] == 1
    assert repo.get_poll_state(connected["id"])["uid_validity"] == "2000"


def test_the_mailbox_own_messages_are_skipped(connected: dict):
    """Otherwise every sent message would be read back as an incoming one."""
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        8: _raw_message(subject="mine", from_address=ADDRESS),
        9: _raw_message(subject="theirs", from_address="recruiter@acme.test"),
    }
    assert inbox.poll_account(connected)["polled"] == 1


def test_the_connected_folder_is_the_one_read(seeker_id: str):
    """Connecting with a sub-folder seeds the cursor, so the first poll reads it."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.imap_basic import connect

    FakeIMAP.folders = ("INBOX", "lkdn-glassdoor")
    account = connect(
        seeker_id,
        address=ADDRESS,
        password=APP_PASSWORD,
        provider="yahoo",
        folder="lkdn-glassdoor",
    )
    FakeIMAP.messages = {3: _raw_message(subject="alert", from_address="jobs@linkedin.test")}
    inbox.poll_account(repo.get_account_any(account["id"]))

    assert FakeIMAP.instances[-1].selected == "lkdn-glassdoor"


def test_an_unmatched_message_becomes_a_reply_row_never_an_opportunity(connected: dict):
    """A job alert is recorded and classified; it is not a source of vacancies."""
    from dreamjob.db.connection import query_all
    from dreamjob.mail import inbox

    FakeIMAP.messages = {
        2: _raw_message(
            subject="Jobs for you", from_address="jobs-listings@linkedin.test", body="5 new jobs"
        )
    }
    inbox.poll_account(connected)

    replies = query_all("SELECT * FROM incoming_reply", ())
    assert len(replies) == 1
    # It matched no dispatch Dream Job sent, so it hangs off nothing.
    assert replies[0]["dispatch_id"] is None
    assert query_all("SELECT * FROM opportunity", ()) == []


def test_a_failing_mailbox_is_recorded_and_does_not_raise(connected: dict):
    """One bad mailbox must not stop a run that has others to poll."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    FakeIMAP.password = "rotated-at-the-provider"
    result = inbox.poll_account(connected)

    assert result["polled"] == 0
    assert "error" in result
    assert repo.get_poll_state(connected["id"])["last_error"]


def test_a_webhook_backend_is_skipped_with_a_reason(seeker_id: str):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    account_id = repo.upsert_account(
        seeker_id, backend="resend", address="relay@stepvda.test", credentials_enc=b"x"
    )
    result = inbox.poll_account(repo.get_account_any(account_id))

    assert result["polled"] == 0
    assert "no mailbox to poll" in result["skipped"]


def test_poll_all_covers_both_personal_backends_on_one_pass(seeker_id: str, connected: dict):
    """A job seeker with a Gmail and a Yahoo gets both read, and the relay skipped."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox

    repo.upsert_account(
        seeker_id, backend="resend", address="relay@stepvda.test", credentials_enc=b"x"
    )
    repo.upsert_account(
        seeker_id, backend="gmail_oauth", address="seeker@gmail.test", credentials_enc=b"x"
    )
    polled = {a["backend"] for a in repo.accounts_to_poll(inbox.pollable_backends())}

    assert polled == {"imap_basic", "gmail_oauth"}


# ---------------------------------------------------------------------------
# Sending (FR-325)
# ---------------------------------------------------------------------------


@pytest.fixture
def armed_transport() -> Iterator[None]:
    """Disarm the dry run, for the tests that mean to reach the transport.

    The guard reads the setting at call time, so flipping it needs no
    re-registration - which is exactly what makes this fixture enough.
    """
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "false"
    get_settings.cache_clear()
    yield
    os.environ["DREAMJOB_MAIL_DRY_RUN"] = "true"
    get_settings.cache_clear()


def test_the_dry_run_guard_covers_this_backend(connected: dict):
    """RK-05: a new transport must not be a way around the dry run.

    The guard wraps every backend class defined in ``dreamjob.mail``, so this
    one is covered by being there - but that is worth asserting, because the
    cost of being wrong is a message leaving the machine unintentionally.
    """
    from dreamjob.mail.base import get_backend
    from dreamjob.mail.dry_run import TransportBlocked, guarded_backends

    assert "imap_basic" in guarded_backends()
    with pytest.raises(TransportBlocked):
        get_backend("imap_basic", connected).send(_message())
    # Nothing reached the provider.
    assert FakeSMTP.sent == []


def _message() -> object:
    from dreamjob.mail.base import OutgoingMessage

    return OutgoingMessage(
        to_email="recruiter@acme.test",
        subject="Introduction",
        body_text="Bonjour,\n\nStephane",
        message_id="<composed@dreamjob.test>",
    )


def test_send_goes_out_over_smtp_as_the_job_seeker(connected: dict, armed_transport: None):
    from dreamjob.mail.base import get_backend

    result = get_backend("imap_basic", connected).send(_message())

    assert result.ok
    assert result.backend == "imap_basic"
    # SMTP does not rewrite the id, so the one in the send log is the one that
    # will come back in a reply's References (FR-326).
    assert result.message_id == "<composed@dreamjob.test>"
    assert FakeSMTP.instances[-1].logged_in_as == ADDRESS
    assert (
        FakeSMTP.sent[-1]["From"].endswith(f"<{ADDRESS}>") or ADDRESS in FakeSMTP.sent[-1]["From"]
    )


def test_a_rotated_password_reports_setup_not_a_transport_failure(
    connected: dict, armed_transport: None
):
    from dreamjob.mail.base import MailBackendNotConfigured, get_backend

    FakeSMTP.password = "rotated-at-the-provider"
    with pytest.raises(MailBackendNotConfigured, match="refused the app password"):
        get_backend("imap_basic", connected).send(_message())


def test_status_reports_the_mailbox_without_the_password(connected: dict):
    from dreamjob.mail.base import get_backend

    status = get_backend("imap_basic", connected).status()

    assert status["configured"] is True
    assert status["address"] == ADDRESS
    assert status["imap_host"] == "imap.mail.yahoo.com"
    assert APP_PASSWORD not in repr(status)


def test_status_of_an_unconnected_backend_points_at_the_connect_route():
    from dreamjob.mail.base import get_backend

    status = get_backend("imap_basic", None).status()

    assert status["configured"] is False
    assert status["connect_path"] == "/api/mail/imap/connect"
    assert "yahoo" in status["providers"]


def test_disconnect_forgets_the_password_and_says_to_revoke_it_too(connected: dict):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.imap_basic import disconnect

    result = disconnect(connected)

    assert result["revoked_locally"] is True
    # There is no provider endpoint to call, and the result must not pretend.
    assert result["revoked_at_provider"] is False
    assert "login.yahoo.com" in result["revoke_hint"]
    assert repo.get_account_any(connected["id"])["credentials_enc"] is None


# ---------------------------------------------------------------------------
# Capability wiring
# ---------------------------------------------------------------------------


def test_the_backend_is_registered_and_declares_itself_pollable():
    from dreamjob.mail.base import backend_class, pollable_backends

    assert "imap_basic" in pollable_backends()
    capabilities = backend_class("imap_basic").capabilities
    # Section 2.4: replies land in the job seeker's own inbox, same as Gmail.
    assert capabilities.replies_to_seeker_inbox is True
    assert capabilities.requires_oauth is False


def test_a_personal_mailbox_outranks_the_relay_for_sending(seeker_id: str, connected: dict):
    from dreamjob.db.repositories import dispatch as repo

    repo.upsert_account(
        seeker_id, backend="resend", address="relay@stepvda.test", credentials_enc=b"x"
    )
    assert repo.active_account(seeker_id)["backend"] == "imap_basic"
