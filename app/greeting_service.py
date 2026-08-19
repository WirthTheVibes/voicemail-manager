"""
Shared orchestration for the voicemail-greeting feature: listing/activating/
deleting greeting files, and starting a phone-based recording. Used by both
the web Settings modal (routes/greetings.py) and the Yealink XML Browser's
"Voicemail Greeting" menu (routes/yealink.py) -- the underlying operations
are identical either way, just triggered from a JSON POST vs a GET-only
phone screen, so the logic lives here once rather than being duplicated.

All greeting *metadata* (which files exist, which one is active) comes from
3CX's own REST xapi (see threecx_notify.py's greeting-file-manager
functions) -- there is no local bookkeeping of "the recording" the way an
earlier version of this module tried to do with a DB row and direct
greetings.xml edits. 3CX already tracks the full file list per extension
(everything recorded through this app, plus anything pre-existing like
sample.wav/test.wav from testing directly in 3CX's own webclient); this
module just reads that instead of duplicating it. Only the actual WAV bytes
are read locally, straight off disk (config.resolve_greeting_path), since
that's faster than round-tripping through 3CX for playback.
"""
import logging
import threading
import time
import wave
from datetime import datetime

from . import config, threecx_notify

logger = logging.getLogger(__name__)

# How long to wait for the phone-based recording to finish before giving up
# -- long enough to answer the ring and speak a greeting without rushing.
RECORD_WAIT_TIMEOUT_SECONDS = 300
RECORD_POLL_INTERVAL_SECONDS = 2

# In-process only (single vm-manager instance, see main.py) -- tracks the
# background poll kicked off by the most recent start_recording() call per
# extension, so recording_status() has something to report regardless of
# which surface (web or Yealink) is asking. Keyed by extension; a fresh
# start_recording() simply replaces the previous entry (see
# _poll_for_recording's filename check for why a stale poll can't clobber
# a newer one).
_pending: dict[str, dict] = {}


class GreetingActionError(Exception):
    """A greeting action couldn't be completed -- message is safe to show
    directly to the user (web error text or a Yealink TextScreen)."""


def _is_playable_recording(path) -> bool:
    """True only if `path` is a real, non-empty WAV this app can actually
    play -- not just "a file exists there". 3CX's own greeting-file list
    (threecx_notify.list_greeting_files) can name a file before it's
    actually a finished recording -- e.g. a RecordFile RPC that got fired
    (see notify_record_file) but the call was never answered or the
    greeting was never actually spoken/saved. Selecting or activating a
    filename like that fails on 3CX's side (400 from the xapi PATCH), so
    nothing downstream should ever be able to see it as an option in the
    first place -- see list_files' filtering and set_active's guard."""
    try:
        with wave.open(str(path), "rb") as w:
            return w.getnframes() > 0
    except (wave.Error, EOFError, OSError):
        return False


def list_files(extension: str) -> list[str]:
    """Every greeting WAV 3CX has stored for `extension` that this app can
    actually see and play -- see _is_playable_recording for why the raw
    xapi list (threecx_notify.list_greeting_files) isn't returned as-is."""
    try:
        filenames = threecx_notify.list_greeting_files(extension)
    except threecx_notify.NotifyError as e:
        raise GreetingActionError(str(e)) from e
    return [f for f in filenames if resolve_path(extension, f) is not None]


def get_active_filename(extension: str) -> str | None:
    """None means 3CX's system default is active (no override)."""
    try:
        return threecx_notify.get_active_greeting_filename(extension)
    except threecx_notify.NotifyError as e:
        raise GreetingActionError(str(e)) from e


def get_state(extension: str) -> dict:
    return {
        "active_filename": get_active_filename(extension),
        "files": list_files(extension),
    }


def resolve_path(extension: str, filename: str):
    """Local filesystem path for one of `extension`'s own greeting files,
    or None if it isn't actually there as a playable recording yet (xapi's
    list can name a file before this app can legitimately see it -- see
    _is_playable_recording)."""
    try:
        path = config.resolve_greeting_path(extension, filename)
    except ValueError:
        return None
    return path if path.is_file() and _is_playable_recording(path) else None


def resolve_active_path(extension: str):
    """The file behind whichever greeting is currently active, or 3CX's
    system default prompt if there's no override -- None if that's missing
    too (see config.SYSTEM_DEFAULT_GREETING_PATH)."""
    filename = get_active_filename(extension)
    if not filename:
        return config.SYSTEM_DEFAULT_GREETING_PATH if config.SYSTEM_DEFAULT_GREETING_PATH.is_file() else None
    return resolve_path(extension, filename)


def set_active(extension: str, filename: str | None) -> None:
    """filename=None (or "") switches back to 3CX's system default. Refuses
    to activate anything this app can't legitimately see as a real
    recording yet, rather than letting 3CX's xapi reject it with an opaque
    400 -- see _is_playable_recording."""
    if filename and resolve_path(extension, filename) is None:
        raise GreetingActionError(f"{filename} isn't available yet -- it may not have finished recording")
    try:
        threecx_notify.set_active_greeting_filename(extension, filename or "")
    except threecx_notify.NotifyError as e:
        raise GreetingActionError(str(e)) from e


def delete_file(extension: str, filename: str) -> None:
    """Permanently deletes one greeting file via 3CX's own xapi. If it was
    the active one, also clears the now-dangling pointer back to 3CX's
    system default -- 3CX's own delete endpoint does not do that itself
    (confirmed against the real webclient's own delete flow, which issues
    the same two calls -- see threecx_notify.delete_greeting_file)."""
    was_active = get_active_filename(extension) == filename
    try:
        threecx_notify.delete_greeting_file(extension, filename)
    except threecx_notify.NotifyError as e:
        raise GreetingActionError(str(e)) from e
    if was_active:
        set_active(extension, None)


def _poll_for_recording(extension: str, filename: str) -> None:
    """Runs on its own thread, off any request/response cycle: 3CX calls
    the extension and records over the phone itself once the RecordFile RPC
    fires -- this just watches for the WAV to land at the path that RPC
    named. Does NOT activate it -- recording only adds a new file to the
    list (see module docstring); the user picks it from Manage Greetings
    like any other file, same as 3CX's own webclient never auto-activates a
    fresh recording either."""
    path = config.resolve_greeting_path(extension, filename)
    deadline = time.monotonic() + RECORD_WAIT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.is_file() and _is_playable_recording(path):
            if _pending.get(extension, {}).get("filename") == filename:
                _pending[extension] = {"filename": filename, "status": "done"}
            return
        time.sleep(RECORD_POLL_INTERVAL_SECONDS)
    if _pending.get(extension, {}).get("filename") == filename:
        _pending[extension] = {"filename": filename, "status": "timed_out"}


def start_recording(extension: str) -> str:
    """Kicks off 3CX's own phone-based recording flow, matching its
    Settings > Greetings "Record" dialog: fires the RecordFile RPC (method
    144, see threecx_notify.notify_record_file), which is what makes 3CX
    call the extension so its owner can record over the phone. Doesn't wait
    for the call or the recording -- returns the generated filename
    immediately, and a background thread polls for the file 3CX writes once
    the user finishes (see recording_status())."""
    filename = f"{extension}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}.wav"
    try:
        threecx_notify.notify_record_file(extension, filename)
    except threecx_notify.NotifyError as e:
        logger.exception("RecordFile RPC failed for extension %s (file %s)", extension, filename)
        raise GreetingActionError("Could not start the recording call") from e

    _pending[extension] = {"filename": filename, "status": "waiting"}
    threading.Thread(target=_poll_for_recording, args=(extension, filename), daemon=True).start()
    return filename


def recording_status(extension: str) -> dict:
    pending = _pending.get(extension)
    if not pending:
        return {"status": "none"}
    return pending
