"""
Server -> browser push for live UI updates (voicemail counters, message
list, heard/reviewed status) when more than one person has the app open at
once. Backed by Server-Sent Events rather than WebSockets: every event here
flows one direction (server to browser), and SSE rides on a plain HTTP
response, which is simpler to reason about than a WS upgrade for this.

Publishers -- routes/messages.py's mutation endpoints and
voicemail_watcher's new-message dispatch -- may run on the event loop
thread (async route) or off it (sync `def` routes run in FastAPI's
threadpool; voicemail_watcher runs on its own daemon thread). publish()
is safe from any of those: it hands the event to the loop via
call_soon_threadsafe rather than touching subscriber queues directly.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)


class EventBroadcaster:
    def __init__(self):
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Called once from main.py's lifespan, on the loop FastAPI actually
        serves requests on -- publish() needs this to safely hand events
        over from other threads."""
        self._loop = loop

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(self._deliver, event)

    def _deliver(self, event: dict) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # A subscriber that isn't draining its queue (dead tab, slow
                # network) shouldn't block or lose events for everyone else --
                # the frontend already re-fetches full state on connect/
                # reconnect, so a dropped event just means it catches up a
                # little later instead of via this specific push.
                logger.warning("Dropping event for a slow SSE subscriber: %s", event.get("type"))


broadcaster = EventBroadcaster()
