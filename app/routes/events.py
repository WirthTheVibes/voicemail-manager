import asyncio
import json
import logging

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from .. import access
from ..deps import get_session
from ..events import broadcaster

logger = logging.getLogger(__name__)

router = APIRouter()

KEEPALIVE_SECONDS = 15


@router.get("/api/events")
async def stream_events(request: Request, session: dict = Depends(get_session)):
    """One SSE connection per open browser tab. Events are filtered to the
    mailboxes this session can access (computed once, at connect time --
    same staleness tolerance as everything else access-related here, and a
    permission change takes effect on the tab's next reconnect either way).

    A message-less event (mailbox is None) is delivered to everyone; none
    exist yet, but the filter is written to allow one without a routing
    change later."""
    accessible = {m["extension"] for m in access.accessible_mailboxes(session)}
    queue = broadcaster.subscribe()

    async def gen():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except asyncio.TimeoutError:
                    yield ": keep-alive\n\n"
                    continue
                if event.get("mailbox") is not None and event["mailbox"] not in accessible:
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={
            # Nginx buffers proxied responses by default, which would hold
            # every event until the buffer filled instead of streaming them
            # -- see the nginx snippet installed by install.sh (referenced
            # in main.py's app_shell docstring) for the reverse-proxy path
            # this app is normally served behind.
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
