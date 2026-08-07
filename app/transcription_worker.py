"""
Auto-transcription for newly-arrived voicemails only -- deliberately no
backfill of whatever's already sitting untranscribed in s_voicemail (that's
what POST /api/messages/{id}/transcribe, the detail panel's "Generate
transcription" button, is for). Two reasons: turning transcription on for
the first time on a mailbox with years of history shouldn't silently kick
off a long unattended run through all of it, and a message that fails here
should require a deliberate retry rather than being silently re-attempted
forever on some interval.

No thread of its own: handle_new_message is registered as a
voicemail_watcher handler (see main.py's lifespan) and runs on *that*
thread, synchronously, as each new message is dispatched -- see
voicemail_watcher.py's own docstring for why one poller feeding multiple
handlers beats every workflow polling s_voicemail separately.
"""
import logging

from . import app_db, config, threecx_db
from .transcription import TranscriptionError, transcribe

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [transcription] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class TranscriptionWorker:
    def __init__(self):
        self._failed_this_run: set[int] = set()

    def handle_new_message(self, message: dict) -> None:
        """Registered as a voicemail_watcher handler (see main.py's
        lifespan) -- transcribes a just-arrived message as it's dispatched.
        This is the only automatic trigger now (see module docstring); a
        failure here is logged and left alone rather than retried, same as
        anything pre-existing -- both wait for a manual
        POST /api/messages/{id}/transcribe."""
        if message["id"] in self._failed_this_run:
            return
        if app_db.get_transcription_settings()["enabled"]:
            self._process_one(message)

    def _process_one(self, message: dict) -> None:
        message_id = message["id"]
        try:
            path = config.resolve_voicemail_path(message["callee"], message["wav_file"])
        except ValueError:
            logger.warning("Skipping message %s: invalid audio path", message_id)
            self._failed_this_run.add(message_id)
            return
        if not path.is_file():
            logger.warning("Skipping message %s: audio file not found at %s", message_id, path)
            self._failed_this_run.add(message_id)
            return
        try:
            result = transcribe(str(path))
        except TranscriptionError as e:
            logger.warning("Transcription failed for message %s: %s", message_id, e)
            self._failed_this_run.add(message_id)
            return
        threecx_db.set_transcription(message_id, result["text"])
        if result["words"]:
            app_db.set_transcription_words(message_id, result["words"])
        logger.info("Transcribed message %s (%d chars, %d words)", message_id, len(result["text"]), len(result["words"]))


transcription_worker = TranscriptionWorker()
