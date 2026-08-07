"""
Central "a voicemail just arrived" watching engine.

3CX gives this app no push/webhook for new voicemails (see
transcription_worker.py's docstring on the same limitation), so this polls
s_voicemail for rows past the last one it's seen and fan them out to
whatever workflows are registered -- notifications.dispatch,
transcription's immediate-trigger, and anything added later. One place
polls; everything else just reacts.

Mirrors phone_service.py / transcription_worker.py's thread-per-concern
model: a single daemon thread, its own logger, its own error handling per
tick so a bad poll doesn't kill the loop.

The high-water mark (app_db.watcher_state.last_seen_id) is advanced after
each message is dispatched, not once per batch -- so a crash mid-batch
never replays a message whose handlers already ran. It does mean a handler
that raises still counts that message as "seen"; retries are each
workflow's own problem (transcription_worker already re-scans for anything
still missing a transcription regardless of this watcher, and
notifications are deliberately best-effort -- see notifications.py).
"""
import logging
import threading
from typing import Callable

from . import app_db, config, threecx_db

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [watcher] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

Handler = Callable[[dict], None]


class VoicemailWatcher:
    def __init__(self):
        self._stop = threading.Event()
        self._handlers: list[Handler] = []

    def register(self, handler: Handler) -> None:
        """Adds a workflow to run against every newly-arrived voicemail.
        Must be called before start() -- handlers run on the watcher's own
        thread, so each one should be quick or hand off its own work to
        another thread (see notifications.dispatch)."""
        self._handlers.append(handler)

    def start(self):
        threading.Thread(target=self._loop, name="voicemail-watcher", daemon=True).start()

    def _seed_if_uninitialized(self):
        # last_seen_id == 0 only ever means "this app has never polled
        # before" (s_voicemail ids start at 1) -- fast-forward to 3CX's
        # current max id so a brand-new install doesn't treat 1500+ years
        # of history as newly-arrived voicemails. A real gap (last_seen_id
        # already past 0) is left alone; that's meant to be caught up on.
        try:
            if app_db.get_watcher_last_seen_id() == 0:
                current_max = threecx_db.max_message_id()
                app_db.set_watcher_last_seen_id(current_max)
                logger.info("First run: starting from id %s instead of replaying history", current_max)
        except Exception:
            logger.exception("Failed to seed watcher high-water mark; will retry next poll")

    def _loop(self):
        self._seed_if_uninitialized()
        while not self._stop.is_set():
            try:
                self._poll_once()
            except Exception:
                logger.exception("Voicemail watcher poll failed")
            self._stop.wait(config.VM_WATCH_POLL_SECONDS)

    def _poll_once(self):
        last_seen = app_db.get_watcher_last_seen_id()
        for message in threecx_db.messages_since(last_seen):
            if self._stop.is_set():
                return
            self._dispatch(message)
            app_db.set_watcher_last_seen_id(message["id"])

    def _dispatch(self, message: dict) -> None:
        for handler in self._handlers:
            try:
                handler(message)
            except Exception:
                name = getattr(handler, "__name__", repr(handler))
                logger.exception("Workflow handler %s failed for message %s", name, message["id"])


voicemail_watcher = VoicemailWatcher()
