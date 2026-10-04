"""Live events API: notifications pushed to the SPA over Server-Sent Events.

The UI used to poll ``/api/monitoring/notifications``; this route keeps one
long-lived ``text/event-stream`` response open per tab instead and pushes what
changed.  SSE rather than a WebSocket because the traffic is one-way, the
browser's ``EventSource`` reconnects by itself, and it carries the session
cookie same-origin with no extra handshake.

The stream does not subscribe to anything: the writers (the vacancy refresh,
the watchlist recheck, the digest, reply detection) run in other tasks or in
another process, so the only shared channel is the database.  The generator
therefore re-reads the notification table every ``POLL_SECONDS`` - a cheap,
indexed query on one seeker - and forwards every kind it finds.

Notifications are private (FR-344): every query filters on ``seeker.id``, and
the session is re-validated about once a minute so a logout, expiry or
suspension (FR-362) also closes streams that are already open.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from dreamjob.api.deps import CurrentSeeker, current_seeker
from dreamjob.db.connection import utcnow
from dreamjob.db.repositories import pipeline_cards as repo

log = logging.getLogger(__name__)

router = APIRouter()

Seeker = Annotated[CurrentSeeker, Depends(current_seeker)]

# Module-level so tests can shrink them instead of waiting real seconds.
POLL_SECONDS = 3.0
HEARTBEAT_SECONDS = 15.0
REVALIDATE_SECONDS = 60.0
# More than a seeker can plausibly receive within one poll; anything beyond it
# in a single tick would be dropped from the stream (still listed by the REST route).
FETCH_LIMIT = 200
# How far before the newest timestamp already sent each poll re-reads, so a row
# committed late with a slightly older ``created_at`` is still pushed.
LOOKBACK_SECONDS = 10

_NOTIFICATION_FIELDS = ("id", "kind", "title", "body", "payload", "severity", "created_at")


def _seconds_before(moment: str, seconds: int) -> str:
    """``moment`` minus ``seconds``, in the canonical ``utcnow()`` text format."""
    parsed = datetime.fromisoformat(moment)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (parsed - timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _sse(event: str, data: dict) -> str:
    """Format one SSE frame.  ``data`` is a single JSON line, so no escaping is needed."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def event_stream(request: Request, seeker_id: str) -> AsyncIterator[str]:
    """Yield SSE frames for one seeker until the client leaves or the session ends.

    Each poll re-reads from :data:`LOOKBACK_SECONDS` before the newest
    timestamp sent (``list_notifications`` is inclusive and second-precision,
    and a concurrent writer can commit a slightly older row late), so rows are
    re-read across ticks; ``seen`` holds the ids inside that window already
    accounted for and stops them being sent twice.  Rows that existed before the stream opened are put in
    ``seen`` up front: the client loads history from the REST route, and the
    stream only announces what is new.  All database reads go through
    ``asyncio.to_thread`` because the repository is synchronous SQLite and the
    serving loop must not block on it.
    """
    loop = asyncio.get_running_loop()
    since = utcnow()
    existing = await asyncio.to_thread(
        repo.list_notifications, seeker_id,
        since=_seconds_before(since, LOOKBACK_SECONDS), limit=FETCH_LIMIT,
    )
    seen = {row["id"] for row in existing}
    unread = await asyncio.to_thread(repo.unread_count, seeker_id)
    yield _sse("hello", {"unread": unread, "at": since})

    last_ping = last_check = loop.time()
    while True:
        await asyncio.sleep(POLL_SECONDS)
        if await request.is_disconnected():
            break

        now = loop.time()
        if now - last_check >= REVALIDATE_SECONDS:
            last_check = now
            try:
                again = await asyncio.to_thread(current_seeker, request)
            except HTTPException as exc:
                log.info("Event stream for %s closed: %s", seeker_id, exc.detail)
                break
            if again.id != seeker_id:
                break

        # Read a little before the high-water mark: a concurrent writer can
        # commit a row stamped a second or two earlier than one already sent,
        # and reading from ``since`` alone would never push it.
        window = _seconds_before(since, LOOKBACK_SECONDS)
        rows = await asyncio.to_thread(
            repo.list_notifications, seeker_id, since=window, limit=FETCH_LIMIT
        )
        # The repository orders newest first; the client wants arrival order.
        for row in reversed(rows):
            if row["id"] in seen:
                continue
            seen.add(row["id"])
            yield _sse("notification", {key: row.get(key) for key in _NOTIFICATION_FIELDS})
        if rows:
            since = max(since, max(row["created_at"] for row in rows))
        # Keep only the ids the next window can still return; every one of them
        # is in ``rows``, because the next window starts no earlier than this one.
        horizon = _seconds_before(since, LOOKBACK_SECONDS)
        seen = {row["id"] for row in rows if row["created_at"] >= horizon}

        count = await asyncio.to_thread(repo.unread_count, seeker_id)
        if count != unread:
            unread = count
            yield _sse("unread", {"unread": unread})

        if now - last_ping >= HEARTBEAT_SECONDS:
            last_ping = now
            yield ": ping\n\n"


@router.get("/stream")
async def stream(request: Request, seeker: Seeker) -> StreamingResponse:
    """Open the live stream for the signed-in seeker.

    ``X-Accel-Buffering: no`` stops nginx-style proxies from holding frames
    back, and ``no-cache`` keeps intermediaries from storing a response that
    never ends.  Authentication happens once here through the usual dependency,
    so an anonymous request gets a plain 401 rather than an empty stream.
    """
    return StreamingResponse(
        event_stream(request, seeker.id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
