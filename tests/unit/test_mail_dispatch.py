"""Mail dispatch slice (FR-325, FR-326, FR-327, NFR-204, NFR-302, NFR-702, RK-05).

Everything here runs against a throwaway SQLite file with no network and no
LLM: the backend under test is a fake registered through the same registry the
real ones use, so the guard rails, the send log and the audit trail are
exercised for real while nothing leaves the machine.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import ClassVar

import pytest
from dreamjob.config import get_settings

_ENV_KEYS = (
    "DREAMJOB_DATA_DIR",
    "DREAMJOB_DB_PATH",
    "DREAMJOB_MASTER_KEY",
    "DREAMJOB_SESSION_SECRET",
    "DREAMJOB_ENV",
    "RESEND_API_KEY",
    "GMAIL_CLIENT_ID",
    "GMAIL_CLIENT_SECRET",
)


@pytest.fixture(autouse=True)
def isolated_db(tmp_path: Path) -> Iterator[None]:
    saved = {k: os.environ.get(k) for k in _ENV_KEYS}
    os.environ["DREAMJOB_DATA_DIR"] = str(tmp_path / "data")
    os.environ["DREAMJOB_DB_PATH"] = str(tmp_path / "data" / "test.db")
    os.environ["DREAMJOB_MASTER_KEY"] = base64.urlsafe_b64encode(secrets.token_bytes(32)).decode()
    os.environ["DREAMJOB_SESSION_SECRET"] = secrets.token_urlsafe(32)
    os.environ["DREAMJOB_ENV"] = "development"
    os.environ["RESEND_API_KEY"] = ""
    os.environ["GMAIL_CLIENT_ID"] = ""
    os.environ["GMAIL_CLIENT_SECRET"] = ""
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


# ---------------------------------------------------------------------------
# A backend that records instead of sending
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_backend_class():
    from dreamjob.mail import gmail as _gmail  # noqa: F401 - populates the registry first
    from dreamjob.mail.base import (
        BackendCapabilities,
        MailBackend,
        SendResult,
        register_backend,
    )

    @register_backend
    class FakeBackend(MailBackend):
        capabilities: ClassVar[BackendCapabilities] = BackendCapabilities(
            key="fake",
            display_name="Fake",
            replies_to_seeker_inbox=True,
            bounce_detection="imap",
            reply_detection="poll",
        )
        sent: ClassVar[list] = []
        fail_with: ClassVar[Exception | None] = None

        def sender_address(self) -> str:
            return str(self.account.get("address") or "seeker@example.test")

        def status(self) -> dict:
            return {"backend": "fake", "configured": True, "reason": None}

        def send(self, message):
            self.check_message(message)
            if FakeBackend.fail_with:
                raise FakeBackend.fail_with
            from dreamjob.mail.composer import to_mime

            FakeBackend.sent.append((message, to_mime(message)))
            return SendResult(
                message_id=message.message_id,
                thread_id=f"thread-{len(FakeBackend.sent)}",
                status="sent",
                backend="fake",
                detail={"fake": True},
            )

    FakeBackend.sent = []
    FakeBackend.fail_with = None
    return FakeBackend


# ---------------------------------------------------------------------------
# Fixtures: one seeker, one approved package addressed to a Belgian company
# ---------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path, fake_backend_class):
    from dreamjob.db.connection import insert_row, utcnow

    now = utcnow()
    seeker_id = insert_row(
        "job_seeker",
        {
            "email": "seeker@example.test",
            "display_name": "Stephane van der Aa",
            "locale": "en",
            "created_at": now,
            "updated_at": now,
        },
    )
    profile_id = insert_row(
        "profile_version",
        {"job_seeker_id": seeker_id, "version": 1, "sections": "{}", "created_at": now},
    )
    directive_id = insert_row(
        "directive_set", {"job_seeker_id": seeker_id, "name": "d", "created_at": now}
    )
    campaign_id = insert_row(
        "campaign",
        {
            "job_seeker_id": seeker_id,
            "directive_set_id": directive_id,
            "profile_version_id": profile_id,
            "name": "c",
            "created_at": now,
        },
    )
    company_id = insert_row(
        "company",
        {
            "normalised_name": "acme",
            "name": "Acme NV",
            "country": "BE",
            "locations": '["Brussels, Belgium"]',
            "collected_at": now,
        },
    )
    contact_id = insert_row(
        "contact",
        {
            "company_id": company_id,
            "full_name": "Marie Dupont",
            "role_title": "Head of Data",
            "email": "marie@acme.test",
            "email_validation": "valid",
            "collected_at": now,
        },
    )
    opportunity_id = insert_row(
        "opportunity",
        {
            "job_seeker_id": seeker_id,
            "campaign_id": campaign_id,
            "company_id": company_id,
            "kind": "speculative",
            "title": "Head of Analytics",
            "language": "en",
            "created_at": now,
            "updated_at": now,
        },
    )

    cv = tmp_path / "cv.pdf"
    cv.write_bytes(b"%PDF-1.4 tailored cv")
    briefing = tmp_path / "briefing.pdf"
    briefing.write_bytes(b"%PDF-1.4 briefing")
    motivation = tmp_path / "motivation.pdf"
    motivation.write_bytes(b"%PDF-1.4 motivation")

    package_id = insert_row(
        "application_package",
        {
            "job_seeker_id": seeker_id,
            "opportunity_id": opportunity_id,
            "contact_id": contact_id,
            "language": "en",
            "cv_pdf_path": str(cv),
            "briefing_pdf_path": str(briefing),
            "motivation_pdf_path": str(motivation),
            "email_subject": "Head of Analytics at Acme",
            "email_body": "Dear Marie,\n\nI am writing about the analytics team you are building.",
            "status": "approved",
            "approved_at": now,
            "approved_by": seeker_id,
            "profile_version_id": profile_id,
            "created_at": now,
            "updated_at": now,
        },
    )
    insert_row(
        "mail_account",
        {
            "job_seeker_id": seeker_id,
            "backend": "fake",
            "address": "seeker@example.test",
            "credentials_enc": b"x",
            "connected_at": now,
        },
    )
    return {
        "seeker_id": seeker_id,
        "package_id": package_id,
        "opportunity_id": opportunity_id,
        "contact_id": contact_id,
        "company_id": company_id,
        "cv": cv,
        "briefing": briefing,
        "motivation": motivation,
        "backend": fake_backend_class,
    }


# ---------------------------------------------------------------------------
# FR-321 / NFR-302 / RK-05 - what the message is allowed to contain
# ---------------------------------------------------------------------------


def test_only_the_cv_is_attached(world):
    """FR-321: the briefing and motivation PDFs are for the job seeker only."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import composer

    package = repo.package_for_dispatch(world["package_id"], world["seeker_id"])
    package["seeker_display_name"] = "Stephane van der Aa"
    attachments = composer.collect_attachments(package)

    assert [a.filename for a in attachments] == ["CV-Stephane-van-der-Aa.pdf"]
    sent_bytes = attachments[0].content
    assert b"tailored cv" in sent_bytes
    assert b"briefing" not in sent_bytes
    assert not any("briefing" in (a.source_path or "") for a in attachments)
    assert not any("motivation" in (a.source_path or "") for a in attachments)


def test_a_cv_field_pointing_at_the_briefing_is_refused(world):
    """FR-321 is enforced on the value, not on the caller's good intentions."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import composer

    package = repo.package_for_dispatch(world["package_id"], world["seeker_id"])
    package["cv_pdf_path"] = package["briefing_pdf_path"]
    package["cv_docx_path"] = None

    with pytest.raises(composer.AttachmentRefused):
        composer.collect_attachments(package)


def test_a_declared_cv_that_is_missing_from_disk_is_refused(world):
    """FR-321: an application must never go out without its CV."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import composer

    package = repo.package_for_dispatch(world["package_id"], world["seeker_id"])
    world["cv"].unlink()

    with pytest.raises(composer.AttachmentRefused, match="missing from disk"):
        composer.collect_attachments(package)


def test_a_package_with_no_declared_cv_sends_without_an_attachment(world):
    """Only a declared path that cannot be resolved is a refusal; none at all is not."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import composer

    package = repo.package_for_dispatch(world["package_id"], world["seeker_id"])
    package["cv_pdf_path"] = None
    package["cv_docx_path"] = None

    assert composer.collect_attachments(package) == []


@pytest.mark.parametrize(
    ("language", "needle"),
    [("en", "no thank you"), ("nl", "liever niet"), ("fr", "non merci"), ("de", "nein danke")],
)
def test_objection_sentence_is_in_the_language_of_the_message(language, needle):
    """NFR-302: every introduction e-mail carries the objection sentence."""
    from dreamjob.mail import composer

    body = composer.build_body(
        "Hello.",
        language=language,
        display_name="S. van der Aa",
        reply_address="seeker@example.test",
    )
    # The body is wrapped for plain text, so compare on collapsed whitespace.
    flat = " ".join(body.lower().split())
    assert needle in flat
    assert "seeker@example.test" in body


def test_message_is_plain_text_with_threading_headers(world):
    """RK-05: no HTML part.  FR-326: the ids a reply will quote are set here."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import composer

    package = repo.package_for_dispatch(world["package_id"], world["seeker_id"])
    message = composer.compose(
        package,
        seeker={"email": "seeker@example.test", "display_name": "Stephane van der Aa"},
        from_email="seeker@example.test",
        in_reply_to="<parent@example.test>",
    )
    mime = composer.to_mime(message)

    types = {part.get_content_type() for part in mime.walk()}
    assert "text/html" not in types
    assert mime.get_content_maintype() == "multipart"  # text + one attachment
    assert mime["Message-ID"] == message.message_id
    assert mime["In-Reply-To"] == "<parent@example.test>"
    assert mime["References"] == "<parent@example.test>"


# ---------------------------------------------------------------------------
# FR-325 - the guard rails
# ---------------------------------------------------------------------------


def test_timezone_comes_from_the_company_not_the_sender():
    from dreamjob.mail.dispatcher import timezone_for

    assert timezone_for("BE", ["Brussels, Belgium"])[1] == "Europe/Brussels"
    # A city beats the country code: a US company is not one time zone.
    assert timezone_for("US", ["San Francisco, CA"])[1] == "America/Los_Angeles"
    assert timezone_for("US", ["New York, NY"])[1] == "America/New_York"
    assert timezone_for("US", [])[1] == "America/New_York"
    assert timezone_for("ZZ", [])[1] == "UTC"


def test_send_window_is_evaluated_in_the_recipients_time_zone(world):
    """FR-325: 09:30 in Brussels is the middle of the night in California."""
    from dreamjob.mail.dispatcher import check_send_allowed

    # A Wednesday, 09:30 Brussels time = 00:30 in Los Angeles.
    moment = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)

    belgian = check_send_allowed(
        world["seeker_id"],
        recipient_email="marie@acme.test",
        email_validation="valid",
        country="BE",
        locations=["Brussels, Belgium"],
        now=moment,
    )
    assert belgian.allowed

    californian = check_send_allowed(
        world["seeker_id"],
        recipient_email="marie@acme.test",
        email_validation="valid",
        country="US",
        locations=["San Francisco, CA"],
        now=moment,
    )
    assert not californian.allowed
    assert californian.code == "outside_window"
    assert californian.deferrable
    # It waits for the recipient's morning, not the sender's.
    assert californian.retry_at is not None
    assert datetime.fromisoformat(californian.retry_at) > moment


def test_weekends_are_outside_the_window(world):
    from dreamjob.mail.dispatcher import check_send_allowed

    saturday = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    decision = check_send_allowed(
        world["seeker_id"],
        recipient_email="marie@acme.test",
        email_validation="valid",
        country="BE",
        now=saturday,
    )
    assert decision.code == "outside_window"
    assert datetime.fromisoformat(decision.retry_at).weekday() == 0  # Monday


@pytest.mark.parametrize("verdict", ["invalid", "unknown", None])
def test_only_valid_or_risky_addresses_are_used(world, verdict):
    """FR-304: 'invalid' addresses shall not be used - nor unvalidated ones."""
    from dreamjob.mail.dispatcher import check_send_allowed

    decision = check_send_allowed(
        world["seeker_id"],
        recipient_email="marie@acme.test",
        email_validation=verdict,
        country="BE",
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    assert not decision.allowed
    assert decision.code == "address_not_sendable"
    assert not decision.deferrable  # waiting will not fix it


def test_an_objection_blocks_the_address_permanently(world):
    """NFR-302: honoured from the shared block list, not from the caller's flag."""
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.mail.dispatcher import check_send_allowed

    contacts_repo.record_objection("marie@acme.test", reason="test")
    decision = check_send_allowed(
        world["seeker_id"],
        recipient_email="marie@acme.test",
        email_validation="valid",
        objected=False,
        country="BE",
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    assert not decision.allowed
    assert decision.code == "objected"
    assert not decision.deferrable


def test_daily_cap_and_pacing_defer_rather_than_refuse(world, monkeypatch):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dispatcher

    moment = datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
    monkeypatch.setattr(get_settings(), "send_daily_cap", 1, raising=False)

    repo.create_dispatch(
        world["seeker_id"],
        {
            "application_package_id": world["package_id"],
            "backend": "fake",
            "recipient_email": "someone@else.test",
            "delivery_status": "sent",
            # Earlier the same day, in the sender's clock, as ``moment``.
            "sent_at": moment.replace(hour=6).isoformat(timespec="seconds"),
        },
    )
    decision = dispatcher.check_send_allowed(
        world["seeker_id"],
        recipient_email="marie@acme.test",
        email_validation="valid",
        country="BE",
        now=moment,
    )
    assert decision.code in ("daily_cap", "rate_limited")
    assert decision.deferrable


# ---------------------------------------------------------------------------
# FR-326 / NFR-702 - the send log and the audit trail
# ---------------------------------------------------------------------------


def test_send_logs_everything_fr326_asks_for(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.dispatcher import send_package
    from dreamjob.security.audit import trail_for_entity

    result = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    assert result["status"] == "sent"

    row = repo.get_dispatch(result["dispatch_id"], world["seeker_id"])
    assert row["recipient_email"] == "marie@acme.test"      # recipient
    assert row["sent_at"]                                   # timestamp
    assert row["attachments"] == [str(world["cv"])]         # attachments
    assert row["message_id"].startswith("<")                # message-id
    assert row["delivery_status"] == "sent"                 # delivery status
    assert row["recipient_timezone"] == "Europe/Brussels"

    # NFR-702: who approved it, and which versions were used.
    trail = trail_for_entity("dispatch", result["dispatch_id"])
    sent_events = [e for e in trail if e["action"] == "application.sent"]
    assert sent_events, trail
    detail = json.loads(sent_events[0]["detail"])
    assert detail["approved_by"] == world["seeker_id"]
    assert detail["profile_version_id"]
    assert detail["attachments"] == ["CV-Stephane-van-der-Aa.pdf"]

    # FR-321, end to end: nothing but the CV is on the wire.
    _message, mime = world["backend"].sent[-1]
    attached = [
        part.get_payload(decode=True)
        for part in mime.walk()
        if part.get_filename()
    ]
    assert attached == [world["cv"].read_bytes()]
    assert not any(b"briefing" in blob or b"motivation" in blob for blob in attached)
    assert "text/html" not in {part.get_content_type() for part in mime.walk()}

    # FR-326: reflected in the opportunity status.
    from dreamjob.db.connection import query_one

    opp = query_one("SELECT user_status FROM opportunity WHERE id = ?", (world["opportunity_id"],))
    assert opp["user_status"] == "applied"


def test_a_send_outside_the_window_is_queued_not_lost(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.dispatcher import send_package

    result = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 2, 0, tzinfo=UTC),  # 04:00 in Brussels
    )
    assert result["status"] == "queued"
    assert result["scheduled_for"]
    # The queued report names the recipient and the attachment, so the UI does
    # not show a blank recipient or claim the CV is missing while it waits.
    assert result["recipient"] == "marie@acme.test"
    assert result["recipient_name"] == "Marie Dupont"
    assert result["subject"] == "Head of Analytics at Acme"
    assert result["attachments"] == ["CV-Stephane-van-der-Aa.pdf"]
    row = repo.get_dispatch(result["dispatch_id"], world["seeker_id"])
    assert row["delivery_status"] == "queued"
    assert world["backend"].sent == []


def test_a_queued_dispatch_does_not_block_a_new_send_and_is_superseded(world):
    """A queued row has not left, so it must not block, and must not send later."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail.dispatcher import send_package

    held = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 2, 0, tzinfo=UTC),  # 04:00 in Brussels: queued
    )
    assert held["status"] == "queued"

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),  # inside the window
    )
    assert sent["status"] == "sent"

    queued_row = repo.get_dispatch(held["dispatch_id"], world["seeker_id"])
    assert queued_row["delivery_status"] == "failed"
    assert queued_row["last_error"] == "superseded by a newer send attempt"
    assert queued_row["delivery_detail"]["superseded"] is True

    assert len(world["backend"].sent) == 1
    # The queue must not also deliver the superseded row.
    assert repo.due_queued(world["seeker_id"]) == []


def test_a_dispatch_with_a_sent_at_blocks_a_resend(world):
    """A message that actually left is the one case that still refuses a resend."""
    from dreamjob.db.connection import update_row
    from dreamjob.mail.dispatcher import SendRefused, send_package

    send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    # A send stamps the package ``sent``; re-approving it isolates the
    # already-dispatched guard from the FR-324 approval guard so the refusal
    # under test is the one that fires.
    update_row("application_package", world["package_id"], {"status": "approved"})

    with pytest.raises(SendRefused, match="already dispatched"):
        send_package(
            world["package_id"],
            world["seeker_id"],
            approved_by=world["seeker_id"],
            now=datetime(2026, 9, 9, 7, 40, tzinfo=UTC),
        )


def test_an_unapproved_package_is_never_sent(world):
    from dreamjob.db.connection import update_row
    from dreamjob.mail.dispatcher import SendRefused, send_package

    update_row("application_package", world["package_id"], {"status": "draft"})
    with pytest.raises(SendRefused, match="FR-324"):
        send_package(
            world["package_id"],
            world["seeker_id"],
            approved_by=world["seeker_id"],
            now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# FR-326 - reply and bounce detection
# ---------------------------------------------------------------------------


def _dsn(original_message_id: str, recipient: str, status_code: str = "5.1.1") -> bytes:
    """A minimal RFC 3464 delivery status notification."""
    return (
        "From: Mail Delivery Subsystem <mailer-daemon@acme.test>\r\n"
        "To: seeker@example.test\r\n"
        "Subject: Delivery Status Notification (Failure)\r\n"
        "Message-ID: <dsn-1@acme.test>\r\n"
        'Content-Type: multipart/report; report-type=delivery-status; boundary="b1"\r\n'
        "\r\n"
        "--b1\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "Your message could not be delivered.\r\n"
        "--b1\r\n"
        "Content-Type: message/delivery-status\r\n\r\n"
        "Reporting-MTA: dns; acme.test\r\n"
        "\r\n"
        f"Final-Recipient: rfc822; {recipient}\r\n"
        "Action: failed\r\n"
        f"Status: {status_code}\r\n"
        'Diagnostic-Code: smtp; 550 5.1.1 No such user\r\n'
        "--b1\r\n"
        "Content-Type: message/rfc822\r\n\r\n"
        "From: seeker@example.test\r\n"
        f"To: {recipient}\r\n"
        "Subject: Head of Analytics at Acme\r\n"
        f"Message-ID: {original_message_id}\r\n\r\n"
        "body\r\n"
        "--b1--\r\n"
    ).encode()


def test_bounces_are_detected_structurally_not_by_subject(world):
    """FR-326: a DSN is recognised from multipart/report, not from its wording."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]

    parsed = inbox.parse_message(_dsn(original, "marie@acme.test"))
    assert parsed.is_bounce
    assert parsed.bounce.permanent
    assert parsed.bounce.status == "5.1.1"
    assert parsed.bounce.recipient == "marie@acme.test"
    # The original Message-ID, quoted inside the report, is the matching key.
    assert parsed.bounce.original_message_id == original

    outcome = inbox.record_incoming(world["seeker_id"], parsed)
    assert outcome["status"] == "bounced"

    row = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])
    assert row["delivery_status"] == "bounced"
    assert row["bounce_detected_at"]
    assert row["delivery_detail"]["status"] == "5.1.1"


def test_a_transient_4xx_report_does_not_mark_the_dispatch_bounced(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]

    parsed = inbox.parse_message(_dsn(original, "marie@acme.test", status_code="4.2.2"))
    outcome = inbox.record_incoming(world["seeker_id"], parsed)

    assert outcome["status"] == "deferred"
    row = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])
    assert row["delivery_status"] == "sent"
    assert row["bounce_detected_at"]


def _reply(in_reply_to: str, body: str = "Thanks, let us talk next week.") -> bytes:
    return (
        "From: Marie Dupont <marie@acme.test>\r\n"
        "To: seeker@example.test\r\n"
        "Subject: Re: Head of Analytics at Acme\r\n"
        "Message-ID: <reply-1@acme.test>\r\n"
        f"In-Reply-To: {in_reply_to}\r\n"
        f"References: {in_reply_to}\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        f"{body}\r\n"
    ).encode()


def test_replies_are_matched_by_in_reply_to(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]

    outcome = inbox.record_incoming(world["seeker_id"], inbox.parse_message(_reply(original)))
    assert outcome["status"] == "reply"
    assert outcome["dispatch_id"] == sent["dispatch_id"]

    row = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])
    assert row["reply_detected_at"]
    assert row["delivery_status"] == "delivered"
    assert repo.list_replies(world["seeker_id"])[0]["from_address"] == "marie@acme.test"


def test_replies_fall_back_to_recipient_and_subject(world):
    """The headers are gone; the address and the subject still identify the send."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    stripped = (
        b"From: Marie Dupont <marie@acme.test>\r\n"
        b"To: seeker@example.test\r\n"
        b"Subject: Re: Head of Analytics at Acme\r\n"
        b"Message-ID: <no-thread@acme.test>\r\n\r\n"
        b"Sorry, resending from a different client.\r\n"
    )

    outcome = inbox.record_incoming(world["seeker_id"], inbox.parse_message(stripped))
    assert outcome["dispatch_id"] == sent["dispatch_id"]
    assert repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["reply_detected_at"]


def test_an_objection_reply_blocks_the_contact(world):
    """NFR-302: honoured when it is read, not when someone reviews the reply."""
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]

    outcome = inbox.record_incoming(
        world["seeker_id"],
        inbox.parse_message(_reply(original, "No thank you, please remove my details.")),
    )
    assert outcome["status"] == "objection"
    assert contacts_repo.is_objected("marie@acme.test")


def test_a_reply_quoting_our_own_message_is_not_an_objection(world):
    """NFR-302 blocks a contact for ever, so it reads the sender's words only.

    Every reply quotes the message it answers, and ours ends with the sentence
    inviting an objection.  Reading the whole body would turn an interested
    reply into a permanent block on the contact.
    """
    from dreamjob.db.repositories import contacts as contacts_repo
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original_message, _mime = world["backend"].sent[-1]
    original_id = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]

    quoted = "\n".join("> " + line for line in original_message.body_text.splitlines())
    body = (
        "Thanks for writing - I would be glad to talk. Are you free on Thursday?\n\n"
        "On Tue, 9 Sep 2026, Stephane van der Aa wrote:\n" + quoted
    )
    outcome = inbox.record_incoming(world["seeker_id"], inbox.parse_message(_reply(original_id, body)))
    assert outcome["status"] == "reply"
    assert outcome["classification"] is None
    assert not contacts_repo.is_objected("marie@acme.test")

    # The same words, typed by the recipient, still block them.
    assert inbox.is_objection("No thank you.")


def test_an_out_of_office_does_not_count_as_a_reply(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]
    raw = (
        "From: Marie Dupont <marie@acme.test>\r\n"
        "To: seeker@example.test\r\n"
        "Subject: Automatic reply: Head of Analytics at Acme\r\n"
        "Message-ID: <ooo-1@acme.test>\r\n"
        f"In-Reply-To: {original}\r\n"
        "Auto-Submitted: auto-replied\r\n\r\n"
        "I am out of the office until Monday.\r\n"
    ).encode()

    outcome = inbox.record_incoming(world["seeker_id"], inbox.parse_message(raw))
    assert outcome["status"] == "auto_reply"
    assert repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["reply_detected_at"] is None


def test_the_same_message_is_only_recorded_once(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox
    from dreamjob.mail.dispatcher import send_package

    sent = send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    original = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["message_id"]
    raw = _reply(original)
    inbox.record_incoming(world["seeker_id"], inbox.parse_message(raw))
    again = inbox.record_incoming(world["seeker_id"], inbox.parse_message(raw))

    assert again["status"] == "duplicate"
    assert len(repo.list_replies(world["seeker_id"])) == 1


# ---------------------------------------------------------------------------
# FR-327 - follow-ups
# ---------------------------------------------------------------------------


def test_follow_up_falls_due_and_threads_under_the_original(world):
    from dreamjob.db.connection import update_row
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dispatcher

    dispatcher.set_follow_up_days(5)
    # The whole test runs on one fixed timeline.  Every moment that matters is
    # passed in, including to the two `due_follow_ups` queries below: reading
    # the real clock there made the test a time bomb, because the follow-up
    # falls due five days after a fixed send date and "nothing is due yet"
    # stopped holding once that date passed.
    sent = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    parent = repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])
    assert parent["follow_up_due_at"]

    # Nothing is due yet: the day after the send, against a 5-day follow-up.
    assert dispatcher.due_follow_ups(
        world["seeker_id"], now=datetime(2026, 9, 10, 7, 30, tzinfo=UTC)
    ) == []

    update_row(
        "dispatch",
        sent["dispatch_id"],
        {"follow_up_due_at": datetime(2026, 9, 9, 7, 0, tzinfo=UTC).isoformat()},
    )
    due = dispatcher.due_follow_ups(
        world["seeker_id"], now=datetime(2026, 9, 9, 7, 40, tzinfo=UTC)
    )
    assert [d["id"] for d in due] == [sent["dispatch_id"]]

    # No LLM is reachable in the test environment, so the template is used.
    draft = dispatcher.generate_follow_up(sent["dispatch_id"], world["seeker_id"])
    assert draft["body"]
    assert draft["subject"].startswith("Re: ")

    world["backend"].sent.clear()
    outcome = dispatcher.send_follow_up(
        sent["dispatch_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 40, tzinfo=UTC),
    )
    assert outcome["status"] == "sent"
    message, mime = world["backend"].sent[-1]
    assert mime["In-Reply-To"] == parent["message_id"]
    assert message.attachments == []  # the CV went with the original

    child = repo.get_dispatch(outcome["dispatch_id"], world["seeker_id"])
    assert child["kind"] == "follow_up"
    assert child["parent_dispatch_id"] == sent["dispatch_id"]
    assert repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["follow_up_sent_at"]


def test_the_queue_sends_what_the_window_held(world):
    """FR-325: a message held overnight goes out when the recipient's day starts."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dispatcher

    held = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 2, 0, tzinfo=UTC),  # 04:00 in Brussels
    )
    assert held["status"] == "queued"

    outcome = dispatcher.process_queue(
        world["seeker_id"], now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
    )
    assert outcome["results"][0]["status"] == "sent"
    row = repo.get_dispatch(held["dispatch_id"], world["seeker_id"])
    assert row["delivery_status"] == "sent"
    assert row["scheduled_for"] is None
    # The Message-ID promised in the log before the send is the one that went out.
    assert world["backend"].sent[-1][1]["Message-ID"] == row["message_id"]


def test_a_queued_follow_up_sends_the_follow_up_text_not_the_application(world):
    from dreamjob.db.connection import update_row
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dispatcher

    sent = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    update_row(
        "dispatch",
        sent["dispatch_id"],
        {"follow_up_draft": "Just checking my note reached you.", "follow_up_subject": "Re: X"},
    )
    queued = dispatcher.send_follow_up(
        sent["dispatch_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 2, 0, tzinfo=UTC),  # outside the window
    )
    assert queued["status"] == "queued"

    # A second follow-up is refused while the first is still in the queue.
    with pytest.raises(dispatcher.SendRefused, match="already"):
        dispatcher.send_follow_up(
            sent["dispatch_id"], world["seeker_id"], approved_by=world["seeker_id"]
        )

    world["backend"].sent.clear()
    dispatcher.process_queue(world["seeker_id"], now=datetime(2026, 9, 9, 7, 40, tzinfo=UTC))

    message, mime = world["backend"].sent[-1]
    assert "Just checking my note reached you." in message.body_text
    assert "analytics team you are building" not in message.body_text
    assert message.attachments == []
    assert mime["In-Reply-To"] == repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])[
        "message_id"
    ]
    assert repo.get_dispatch(sent["dispatch_id"], world["seeker_id"])["follow_up_sent_at"]
    assert repo.get_dispatch(queued["dispatch_id"], world["seeker_id"])["delivery_status"] == "sent"


def test_a_queued_follow_up_keeps_the_text_the_seeker_edited(world):
    """FR-324/NFR-702: what goes out is what was approved, even after a wait."""
    from dreamjob.db.connection import update_row
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dispatcher

    sent = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    update_row(
        "dispatch",
        sent["dispatch_id"],
        {"follow_up_draft": "The generated draft.", "follow_up_subject": "Re: X"},
    )
    queued = dispatcher.send_follow_up(
        sent["dispatch_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        body="The sentence I actually want to send.",
        now=datetime(2026, 9, 9, 2, 0, tzinfo=UTC),  # outside the window
    )
    assert queued["status"] == "queued"
    assert repo.get_dispatch(queued["dispatch_id"], world["seeker_id"])["follow_up_draft"] == (
        "The sentence I actually want to send."
    )

    world["backend"].sent.clear()
    dispatcher.process_queue(world["seeker_id"], now=datetime(2026, 9, 9, 7, 40, tzinfo=UTC))
    body = world["backend"].sent[-1][0].body_text
    assert "The sentence I actually want to send." in body
    assert "The generated draft." not in body


def test_one_unsendable_dispatch_does_not_stop_the_queue(world):
    """A package the composer refuses fails on its own row, not on the batch."""
    from dreamjob.db.connection import execute
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import dispatcher

    queued = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 2, 0, tzinfo=UTC),
    )
    assert queued["status"] == "queued"
    execute("UPDATE application_package SET email_body = '' WHERE id = ?", (world["package_id"],))

    outcome = dispatcher.process_queue(
        world["seeker_id"], now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC)
    )
    assert outcome["results"][0]["status"] == "failed"
    row = repo.get_dispatch(queued["dispatch_id"], world["seeker_id"])
    assert row["delivery_status"] == "failed"
    assert "FR-324" in row["last_error"]


def test_a_follow_up_reminder_is_raised_once(world):
    """FR-327 asks for a reminder, not one per pass of the scheduler."""
    from dreamjob.db.connection import query_all, update_row
    from dreamjob.mail import dispatcher

    sent = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    update_row(
        "dispatch",
        sent["dispatch_id"],
        {"follow_up_due_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
    )
    assert dispatcher.raise_follow_up_notifications(world["seeker_id"]) == 1
    assert dispatcher.raise_follow_up_notifications(world["seeker_id"]) == 0
    rows = query_all(
        "SELECT id FROM notification WHERE job_seeker_id = ? AND kind = 'follow_up_due'",
        (world["seeker_id"],),
    )
    assert len(rows) == 1


def test_a_model_sign_off_is_stripped_from_the_follow_up():
    """The composer appends the signature; a second one, or a placeholder, must not go out."""
    from dreamjob.mail.dispatcher import _strip_sign_off

    generated = (
        "I wanted to check that my message reached you.\n\n"
        "Kind regards,\n"
        "[Your Name]\n"
    )
    cleaned = _strip_sign_off(generated)
    assert cleaned == "I wanted to check that my message reached you."
    assert "[Your Name]" not in cleaned


def test_no_follow_up_after_a_reply(world):
    from dreamjob.db.connection import update_row
    from dreamjob.mail import dispatcher

    sent = dispatcher.send_package(
        world["package_id"],
        world["seeker_id"],
        approved_by=world["seeker_id"],
        now=datetime(2026, 9, 9, 7, 30, tzinfo=UTC),
    )
    update_row("dispatch", sent["dispatch_id"], {"reply_detected_at": "2026-09-10T09:00:00+00:00"})
    with pytest.raises(dispatcher.SendRefused, match="already had a reply"):
        dispatcher.send_follow_up(
            sent["dispatch_id"], world["seeker_id"], approved_by=world["seeker_id"]
        )


# ---------------------------------------------------------------------------
# Resend - complete, but not configured yet
# ---------------------------------------------------------------------------


def test_resend_reports_itself_as_not_configured_without_a_key():
    from dreamjob.mail.base import MailBackendNotConfigured, OutgoingMessage
    from dreamjob.mail.composer import new_message_id
    from dreamjob.mail.resend_backend import ResendBackend

    backend = ResendBackend()
    state = backend.status()
    assert state["configured"] is False
    assert "RESEND_API_KEY" in state["reason"]
    assert state["from_address"] == "stephane@stepvda.com"

    message = OutgoingMessage(
        to_email="marie@acme.test",
        subject="Hello",
        body_text="Hello.",
        from_email="stephane@stepvda.com",
        message_id=new_message_id("stepvda.com"),
    )
    with pytest.raises(MailBackendNotConfigured, match="RESEND_API_KEY"):
        backend.send(message)


def test_resend_webhook_signature_is_verified():
    from dreamjob.mail import resend_backend

    secret_material = base64.b64encode(b"super-secret-key").decode()
    resend_backend.set_webhook_secret("whsec_" + secret_material)

    body = json.dumps({"type": "email.bounced", "data": {"email_id": "abc"}}).encode()
    msg_id = "msg_2xyz"
    timestamp = str(int(time.time()))
    signed = b".".join([msg_id.encode(), timestamp.encode(), body])
    signature = base64.b64encode(
        hmac.new(base64.b64decode(secret_material), signed, hashlib.sha256).digest()
    ).decode()
    headers = {
        "svix-id": msg_id,
        "svix-timestamp": timestamp,
        "svix-signature": f"v1,{signature}",
    }

    resend_backend.verify_signature(body, headers)  # does not raise

    with pytest.raises(resend_backend.WebhookVerificationError):
        resend_backend.verify_signature(body + b" ", headers)

    stale = {**headers, "svix-timestamp": str(int(time.time()) - 3600)}
    with pytest.raises(resend_backend.WebhookVerificationError, match="replay window"):
        resend_backend.verify_signature(body, stale)


def test_resend_bounce_webhook_marks_the_dispatch(world):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import inbox, resend_backend

    dispatch_id = repo.create_dispatch(
        world["seeker_id"],
        {
            "application_package_id": world["package_id"],
            "backend": "resend",
            "recipient_email": "marie@acme.test",
            "message_id": "<abc@stepvda.com>",
            "thread_id": "resend-id-1",
            "delivery_status": "sent",
            "sent_at": "2026-09-09T08:00:00+00:00",
        },
    )
    event = resend_backend.parse_event(
        {
            "type": "email.bounced",
            "created_at": "2026-09-09T08:01:00Z",
            "data": {
                "email_id": "resend-id-1",
                "to": ["marie@acme.test"],
                "subject": "Head of Analytics at Acme",
                "bounce": {
                    "type": "Permanent",
                    "subType": "General",
                    "message": "550 no such user",
                },
            },
        }
    )
    assert event["permanent"] is True
    outcome = inbox.apply_provider_event(event, delivery_id="msg_1", provider="resend")

    assert outcome["status"] == "bounced"
    row = repo.get_dispatch(dispatch_id, world["seeker_id"])
    assert row["delivery_status"] == "bounced"
    assert row["bounce_detected_at"]
    # A replay of the same delivery changes nothing.
    assert inbox.apply_provider_event(event, delivery_id="msg_1", provider="resend")[
        "status"
    ] == "duplicate"


# ---------------------------------------------------------------------------
# NFR-204 - the OAuth handshake
# ---------------------------------------------------------------------------


def test_gmail_says_what_is_missing_when_it_is_not_set_up(world):
    from dreamjob.mail.base import MailBackendNotConfigured
    from dreamjob.mail.gmail import authorization_url

    with pytest.raises(MailBackendNotConfigured, match="GMAIL_CLIENT_ID"):
        authorization_url(world["seeker_id"])


def test_gmail_authorization_state_is_single_use(world, monkeypatch):
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import gmail

    monkeypatch.setattr(get_settings(), "gmail_client_id", "client-id.apps", raising=False)
    started = gmail.authorization_url(world["seeker_id"])

    assert "code_challenge_method=S256" in started["authorization_url"]
    assert "access_type=offline" in started["authorization_url"]
    # NFR-204: send plus the narrowest usable read scope, and nothing else.
    assert set(started["scopes"]) == {
        "https://www.googleapis.com/auth/gmail.send",
        "https://www.googleapis.com/auth/gmail.readonly",
    }

    first = repo.take_oauth_state(started["state"])
    assert first["job_seeker_id"] == world["seeker_id"]
    assert repo.take_oauth_state(started["state"]) is None


def test_stored_gmail_credentials_are_encrypted(world):
    """NFR-204: the refresh token is never at rest in the clear."""
    from dreamjob.db.connection import query_one
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import gmail

    credentials = {"refresh_token": "1//super-secret-refresh", "scope": " ".join(gmail.SCOPES)}
    blob = gmail._encrypt_credentials(credentials, world["seeker_id"])
    account_id = repo.upsert_account(
        world["seeker_id"],
        backend="gmail_oauth",
        address="seeker@gmail.test",
        credentials_enc=blob,
        scopes=" ".join(gmail.SCOPES),
    )

    stored = query_one("SELECT credentials_enc FROM mail_account WHERE id = ?", (account_id,))
    assert b"super-secret-refresh" not in bytes(stored["credentials_enc"])
    assert gmail._decrypt_credentials(stored["credentials_enc"], world["seeker_id"]) == credentials

    # The public listing never exposes the blob.
    listed = [a for a in repo.list_accounts(world["seeker_id"]) if a["id"] == account_id][0]
    assert "credentials_enc" not in listed
    assert listed["has_credentials"] == 1

    repo.clear_credentials(account_id)
    after = query_one(
        "SELECT credentials_enc, is_active FROM mail_account WHERE id = ?", (account_id,)
    )
    assert after["credentials_enc"] is None
    assert after["is_active"] == 0


def test_mime_round_trips_through_the_parser(world):
    """What the composer builds is what the inbox parser reads back."""
    from dreamjob.db.repositories import dispatch as repo
    from dreamjob.mail import composer, inbox

    package = repo.package_for_dispatch(world["package_id"], world["seeker_id"])
    message = composer.compose(
        package,
        seeker={"email": "seeker@example.test", "display_name": "Stephane van der Aa"},
        from_email="seeker@example.test",
    )
    raw = composer.to_mime(message).as_bytes()
    parsed = inbox.parse_message(raw)

    assert parsed.message_id == message.message_id
    assert parsed.from_address == "seeker@example.test"
    assert not parsed.is_bounce
    assert "analytics team" in parsed.body
    assert "no thank you" in parsed.body.lower()


# ---------------------------------------------------------------------------
# The API surface
# ---------------------------------------------------------------------------


@pytest.fixture
def client(world):
    from dreamjob.api.deps import CurrentSeeker, current_admin, current_seeker
    from dreamjob.api.routers import mail as mail_router
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(mail_router.router, prefix="/api/mail")
    seeker = CurrentSeeker(
        id=world["seeker_id"],
        email="seeker@example.test",
        display_name="Stephane van der Aa",
        is_admin=True,
        locale="en",
    )
    app.dependency_overrides[current_seeker] = lambda: seeker
    app.dependency_overrides[current_admin] = lambda: seeker
    with TestClient(app) as c:
        yield c


def test_status_reports_resend_as_not_configured(client):
    """The key is not available yet, and the status endpoint says so."""
    body = client.get("/api/mail/status").json()
    resend = [b for b in body["backends"] if b["backend"] == "resend"][0]
    assert resend["configured"] is False
    assert "RESEND_API_KEY" in resend["reason"]

    gmail_state = [b for b in body["backends"] if b["backend"] == "gmail_oauth"][0]
    assert gmail_state["configured"] is False
    assert gmail_state["capabilities"]["replies_to_seeker_inbox"] is True


def test_send_check_explains_a_refusal(client, world):
    from dreamjob.db.connection import update_row

    update_row("contact", world["contact_id"], {"email_validation": "invalid"})
    body = client.get(
        "/api/mail/send-check", params={"application_package_id": world["package_id"]}
    ).json()

    assert body["allowed"] is False
    assert body["code"] == "address_not_sendable"
    assert body["timezone"] == "Europe/Brussels"


def test_send_and_read_back_the_log(client, world):
    created = client.post("/api/mail/send", json={"application_package_id": world["package_id"]})
    assert created.status_code == 201, created.text
    # Whether it goes now or waits depends on the recipient's clock (FR-325);
    # either way the send log has it.
    assert created.json()["status"] in ("sent", "queued")

    log = client.get("/api/mail/dispatches").json()
    assert len(log) == 1
    assert log[0]["recipient_email"] == "marie@acme.test"
    assert log[0]["recipient_timezone"] == "Europe/Brussels"


def test_webhook_without_a_signature_is_rejected(client):
    response = client.post("/api/mail/resend/webhook", json={"type": "email.delivered"})
    assert response.status_code == 401


def test_gmail_callback_without_a_state_fails_politely(client):
    response = client.get("/api/mail/gmail/callback", params={"error": "access_denied"})
    assert response.status_code == 400
    assert "access_denied" in response.text


def test_polling_skips_a_backend_that_has_no_mailbox(world):
    """Resend reports by webhook; there is nothing to poll (FR-326)."""
    from dreamjob.mail import inbox

    result = inbox.poll_account({"id": "x", "backend": "resend", "job_seeker_id": "s"})
    assert result["polled"] == 0
    assert "webhook" in result["skipped"]


def test_imap_authentication_string_is_the_xoauth2_form():
    """RFC-shaped SASL string; a wrong one fails at the server with no diagnosis."""
    from dreamjob.mail.inbox import _xoauth2_string

    assert (
        _xoauth2_string("seeker@gmail.test", "ya29.token")
        == "user=seeker@gmail.test\x01auth=Bearer ya29.token\x01\x01"
    )
