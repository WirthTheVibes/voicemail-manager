"""
Voicemail transcription: two selectable engines (Settings > Transcription
tab), "local" (faster-whisper, on this box) or "openai" (OpenAI's hosted
transcription API).

Local always runs in a *subprocess* (transcribe_subprocess.py), never
in-process:

- Loading ctranslate2/faster-whisper costs hundreds of MB resident (varies
  by WHISPER_MODEL_SIZE -- ~320MB measured for the default "tiny" model,
  ~730MB for "small"), which has no business living in the main app's
  event-loop process.
- It lets the memory cap (config.WHISPER_MEMORY_LIMIT_MB, default 1GB) be
  enforced by simply killing that subprocess. An RLIMIT_AS cap on the
  in-process alternative doesn't work: measured live on this box with the
  "small" model, ctranslate2+onnxruntime reserve ~3.3GB of *virtual* address
  space to hold ~730MB of actual resident memory, so a 1GB RLIMIT_AS
  crashes it immediately despite real usage being well under the cap --
  the gap is onnxruntime/ctranslate2 fixed overhead, not model-size-
  proportional, so smaller models don't make RLIMIT_AS viable either.
- This box is the live 3CX PBX -- deliberately not touching cgroups/systemd
  for this (no Delegate=yes, no transient scopes), so the cap is enforced
  entirely in userspace: a thread polls the subprocess's real RSS
  (/proc/<pid>/status) every _WATCHDOG_INTERVAL and SIGKILLs it if it goes
  over. Polling-based, so a big enough spike could very briefly exceed the
  cap before being killed -- not a hard kernel guarantee, but sufficient
  headroom exists (default "tiny" model peaks ~320MB, vs a 1GB cap) that
  this is a safety net, not the normal case.
- preexec_fn arms PR_SET_PDEATHSIG so the subprocess is killed by the kernel
  if this app process dies first (crash, kill -9, systemd restart) instead
  of being orphaned to init.

Writing the result back to s_voicemail.transcription is a plain SQL UPDATE
(threecx_db.set_transcription) -- see that function's docstring for why
that's fine here unlike the `heard` column.

transcribe() returns {"text": str, "words": [{"word","start","end"}, ...]}.
"words" is what drives the detail panel's click-a-word-to-seek /
highlight-as-it-plays transcript (app.js's wireTranscriptWords) -- it's
empty when the engine/model in use doesn't provide word-level timestamps.
The local engine always provides them (word_timestamps=True in
transcribe_subprocess.py). OpenAI only does for the "whisper-1" model --
gpt-4o-transcribe/gpt-4o-mini-transcribe don't support the verbose_json
response format word timestamps require, so OPENAI_TRANSCRIBE_MODEL's
default (gpt-4o-transcribe) gets plain text only; the transcript still
displays fine, just without the karaoke behavior. Callers persist "words"
via app_db.set_transcription_words when non-empty.
"""
import ctypes
import ctypes.util
import json
import logging
import subprocess
import sys
import threading
import time

import requests

from . import app_db, config

logger = logging.getLogger(__name__)

_WATCHDOG_INTERVAL_SECONDS = 0.5
_SUBPROCESS_TIMEOUT_SECONDS = 300

_PR_SET_PDEATHSIG = 1
_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


def _die_with_parent():
    # Runs in the child right after fork(), before exec() -- see subprocess's
    # preexec_fn docs. Only touches libc, so it's safe under the
    # fork()-without-exec() restrictions that rule out most other work here.
    _libc.prctl(_PR_SET_PDEATHSIG, 9)  # SIGKILL


class TranscriptionError(RuntimeError):
    """Transcription failed, or the currently-selected engine isn't usable."""


# Both engines return a normal 200/success with an empty (or whitespace-only)
# "text" for silent/no-speech audio -- there's no exception to catch. transcribe()
# below substitutes this sentinel so the UI (app.js's renderTranscriptionBody,
# matched against this exact string) can tell "ran, found nothing" apart from
# "never run" (message["transcription"] still NULL/empty), which a bare empty
# string can't do on its own.
NO_SPEECH_TEXT = "No speech detected in this recording."


def _vmrss_kb(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except (FileNotFoundError, ProcessLookupError):
        return None
    return None


def _run_local_subprocess(wav_path: str) -> dict:
    proc = subprocess.Popen(
        [sys.executable, "-m", "app.transcribe_subprocess", wav_path],
        cwd=str(config.APP_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        preexec_fn=_die_with_parent,
    )
    limit_kb = config.WHISPER_MEMORY_LIMIT_MB * 1024
    killed = threading.Event()
    watchdog_stop = threading.Event()

    def _watchdog():
        while not watchdog_stop.is_set() and proc.poll() is None:
            rss = _vmrss_kb(proc.pid)
            if rss is not None and rss > limit_kb:
                killed.set()
                proc.kill()
                return
            watchdog_stop.wait(_WATCHDOG_INTERVAL_SECONDS)

    watchdog = threading.Thread(target=_watchdog, name="transcribe-watchdog", daemon=True)
    watchdog.start()
    try:
        stdout, stderr = proc.communicate(timeout=_SUBPROCESS_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        raise TranscriptionError(
            f"Local transcription timed out after {_SUBPROCESS_TIMEOUT_SECONDS}s and was killed"
        )
    finally:
        watchdog_stop.set()
        watchdog.join(timeout=_WATCHDOG_INTERVAL_SECONDS * 2)

    if killed.is_set():
        raise TranscriptionError(
            f"Local transcription exceeded the {config.WHISPER_MEMORY_LIMIT_MB}MB memory cap and was killed"
        )
    if proc.returncode != 0:
        raise TranscriptionError(f"Local transcription failed: {(stderr or '').strip()[-500:] or 'unknown error'}")
    try:
        result = json.loads(stdout.strip())
        return {"text": result["text"], "words": result.get("words", [])}
    except (json.JSONDecodeError, KeyError) as e:
        raise TranscriptionError(f"Local transcription produced unparseable output: {e}") from e


# Only whisper-1 supports word-level timestamps (response_format=verbose_json
# with timestamp_granularities). gpt-4o-transcribe / gpt-4o-mini-transcribe
# don't -- see this module's docstring.
_OPENAI_WORD_TIMESTAMP_MODELS = {"whisper-1"}


def _run_openai(wav_path: str) -> dict:
    if not config.OPENAI_API_KEY:
        raise TranscriptionError("OpenAI transcription is not configured (OPENAI_API_KEY not set in .env)")
    wants_words = config.OPENAI_TRANSCRIBE_MODEL in _OPENAI_WORD_TIMESTAMP_MODELS
    data = {"model": config.OPENAI_TRANSCRIBE_MODEL}
    if wants_words:
        data["response_format"] = "verbose_json"
        data["timestamp_granularities[]"] = "word"
    try:
        with open(wav_path, "rb") as f:
            resp = requests.post(
                "https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
                files={"file": (wav_path.rsplit("/", 1)[-1], f, "audio/wav")},
                data=data,
                timeout=60,
            )
    except requests.RequestException as e:
        raise TranscriptionError(f"OpenAI transcription request failed: {e}") from e
    if resp.status_code != 200:
        raise TranscriptionError(f"OpenAI transcription failed: {resp.status_code} {resp.text[:300]}")
    body = resp.json()
    words = (
        [
            {"word": (w.get("word") or "").strip(), "start": round(w["start"], 2), "end": round(w["end"], 2)}
            for w in body.get("words", [])
        ]
        if wants_words
        else []
    )
    return {"text": (body.get("text") or "").strip(), "words": words}


def is_engine_available(engine: str) -> bool:
    if engine == "openai":
        return bool(config.OPENAI_API_KEY)
    return engine == "local"


def transcribe(wav_path: str, engine: str | None = None) -> dict:
    """Transcribes `wav_path` with `engine`, or whichever engine is currently
    selected in app_db's transcription settings if not given. Returns
    {"text", "words"} -- see this module's docstring. Raises
    TranscriptionError on failure -- callers decide how to surface that."""
    if engine is None:
        engine = app_db.get_transcription_settings()["engine"]
    result = _run_openai(wav_path) if engine == "openai" else _run_local_subprocess(wav_path)
    if not result["text"].strip():
        result["text"] = NO_SPEECH_TEXT
    return result
