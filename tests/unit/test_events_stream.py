"""Live events stream (``GET /api/events/stream``).

The generator is driven directly with a fake request rather than through an
HTTP client: the response never ends by design, and a test client would have
to rely on disconnect detection to stop it.  The poll, heartbeat and
re-validation intervals are module constants, shrunk here to zero so a test
runs in milliseconds.  Everything runs against the session's scratch database
(tests/unit/conftest.py); each test makes its own seeker so rows never mix.

The properties being pinned down:

* an anonymous request gets 401, not an empty stream;
* the stream opens with ``hello`` carrying the unread count;
* a notification written after connect is delivered exactly once, and one
  written before connect is not replayed;
* ``unread`` is sent when the count changes, and only then;
* a session that stops validating ends the stream.
"""

from __future__ import annotations

import asyncio
import json
import secrets

import pytest
from dreamjob.api.routers import events
from dreamjob.db.connection import insert_row, utcnow
from dreamjob.db.repositories import pipeline_cards as repo
from fastapi import HTTPException


@pytest.fixture(autouse=True)
def fast_intervals(monkeypatch: pytest.MonkeyPatch) -> None:
    """Poll on every tick; no heartbeat or re-validation unless a test asks."""
    monkeypatch.setattr(events, "POLL_SECONDS", 0)
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 3600)
    monkeypatch.setattr(events, "REVALIDATE_SECONDS", 3600)


@pytest.fixture
def seeker_id() -> str:
    return insert_row(
        "job_seeker",
        {
            "email": f"events-{secrets.token_hex(4)}@example.test",
            "display_name": "Events Seeker",
            "locale": "en",
            "created_at": utcnow(),
            "updated_at": utcnow(),
        },
    )


class FakeRequest:
    """Stands in for ``Request``: reports a disconnect after ``ticks`` polls."""

    def __init__(self, ticks: int) -> None:
        self.ticks = ticks

    async def is_disconnected(self) -> bool:
        self.ticks -= 1
        return self.ticks < 0


def _parse(frame: str) -> tuple[str, dict | None]:
    """Split an SSE frame into (event name, decoded data); comments give ("comment", None)."""
    if frame.startswith(":"):
        return "comment", None
    lines = dict(line.split(": ", 1) for line in frame.strip().splitlines())
    return lines["event"], json.loads(lines["data"])


def _notify(seeker: str, title: str) -> str:
    return repo.notify(
        seeker,
        {"kind": "new_vacancy", "title": title, "body": "b", "payload": {"x": 1}},
    )


async def _drain(agen, after_hello=None) -> list[tuple[str, dict | None]]:
    """Collect every frame; ``after_hello`` runs once the stream is open."""
    frames = []
    async for frame in agen:
        frames.append(_parse(frame))
        if len(frames) == 1 and after_hello is not None:
            after_hello()
    return frames


def test_anonymous_request_is_refused() -> None:
    from dreamjob.main import create_app
    from fastapi.testclient import TestClient

    with TestClient(create_app()) as http:
        assert http.get("/api/events/stream").status_code == 401


def test_route_answers_with_an_unbuffered_event_stream(seeker_id: str) -> None:
    from dreamjob.api.deps import CurrentSeeker

    seeker = CurrentSeeker(seeker_id, "e@example.test", "E", False, "en")
    response = asyncio.run(events.stream(FakeRequest(0), seeker))
    assert response.media_type == "text/event-stream"
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"


def test_hello_carries_the_unread_count(seeker_id: str) -> None:
    _notify(seeker_id, "older")
    frames = asyncio.run(_drain(events.event_stream(FakeRequest(0), seeker_id)))
    name, data = frames[0]
    assert name == "hello"
    assert data["unread"] == 1
    assert data["at"]
    # The pre-existing notification is history, not news.
    assert [n for n, _ in frames] == ["hello"]


def test_new_notification_is_delivered_once_with_an_unread_update(seeker_id: str) -> None:
    created: list[str] = []
    frames = asyncio.run(
        _drain(
            events.event_stream(FakeRequest(4), seeker_id),
            after_hello=lambda: created.append(_notify(seeker_id, "Data lead at Northwind")),
        )
    )
    names = [n for n, _ in frames]
    assert names == ["hello", "notification", "unread"]
    notification = frames[1][1]
    assert notification["id"] == created[0]
    assert notification["kind"] == "new_vacancy"
    assert notification["title"] == "Data lead at Northwind"
    assert notification["payload"] == {"x": 1}
    assert notification["severity"] == "info"
    assert set(notification) == {"id", "kind", "title", "body", "payload", "severity", "created_at"}
    assert frames[0][1]["unread"] == 0
    assert frames[2][1] == {"unread": 1}


def test_late_commit_with_an_older_timestamp_is_still_delivered(seeker_id: str) -> None:
    """A row committed after connect but stamped before it is news, not history."""
    created: list[str] = []

    def late_writer() -> None:
        stamp = events._seconds_before(utcnow(), 3)
        created.append(
            repo.notify(
                seeker_id,
                {"kind": "new_vacancy", "title": "late", "body": "b", "created_at": stamp},
            )
        )

    frames = asyncio.run(
        _drain(events.event_stream(FakeRequest(3), seeker_id), after_hello=late_writer)
    )
    delivered = [data["id"] for name, data in frames if name == "notification"]
    assert delivered == created


def test_unread_is_sent_when_notifications_are_read(seeker_id: str) -> None:
    _notify(seeker_id, "older")
    frames = asyncio.run(
        _drain(
            events.event_stream(FakeRequest(3), seeker_id),
            after_hello=lambda: repo.mark_all_read(seeker_id),
        )
    )
    assert frames == [("hello", frames[0][1]), ("unread", {"unread": 0})]


def test_heartbeat_comment(seeker_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(events, "HEARTBEAT_SECONDS", 0)
    frames = asyncio.run(_drain(events.event_stream(FakeRequest(1), seeker_id)))
    assert ("comment", None) in frames


def test_expired_session_ends_the_stream(seeker_id: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(request):  # noqa: ANN001, ANN202 - mirrors current_seeker
        raise HTTPException(401, "Session expired")

    monkeypatch.setattr(events, "REVALIDATE_SECONDS", 0)
    monkeypatch.setattr(events, "current_seeker", refuse)
    request = FakeRequest(1000)
    frames = asyncio.run(_drain(events.event_stream(request, seeker_id)))
    assert [n for n, _ in frames] == ["hello"]
    assert request.ticks == 999  # stopped on the first tick, not by disconnect
