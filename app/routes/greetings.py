import logging

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import config, greeting_service, threecx_db
from ..deps import get_session
from ..phone_service import PhoneServiceUnavailable, phone_service

logger = logging.getLogger(__name__)

router = APIRouter()

CHUNK_SIZE = 1024 * 256

# Same bounds as the Yealink phone's own "Change PIN" screen (routes/yealink.py)
# -- kept in sync so a PIN set from either place is valid from the other.
MIN_PIN_LEN = 4
MAX_PIN_LEN = 15


@router.get("/api/greeting")
def get_greeting(session: dict = Depends(get_session)):
    """Always the logged-in user's own greetings -- same scoping as the
    phone-call endpoints below, no extension taken from the caller.
    "active_filename": null means 3CX's system default is playing. "files"
    is every greeting WAV 3CX has stored for this extension (see
    greeting_service.list_files) -- the file manager list."""
    try:
        return greeting_service.get_state(session["extension"])
    except greeting_service.GreetingActionError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))


def _stream_wav_response(path, request: Request | None) -> StreamingResponse:
    file_size = path.stat().st_size
    range_header = request.headers.get("range") if request else None
    if range_header:
        try:
            range_value = range_header.replace("bytes=", "").split("-")
            start = int(range_value[0]) if range_value[0] else 0
            end = int(range_value[1]) if len(range_value) > 1 and range_value[1] else file_size - 1
        except ValueError:
            start, end = 0, file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        def iter_range():
            with open(path, "rb") as f:
                f.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = f.read(min(CHUNK_SIZE, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(length),
        }
        return StreamingResponse(iter_range(), status_code=206, media_type="audio/wav", headers=headers)

    def iter_full():
        with open(path, "rb") as f:
            while True:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                yield chunk

    headers = {"Accept-Ranges": "bytes", "Content-Length": str(file_size)}
    return StreamingResponse(iter_full(), media_type="audio/wav", headers=headers)


def _resolve_file_path(session: dict, filename: str):
    path = greeting_service.resolve_path(session["extension"], filename)
    if path is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Greeting file not found")
    return path


def _resolve_default_path():
    if not config.SYSTEM_DEFAULT_GREETING_PATH.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="3CX's default greeting prompt is not available")
    return config.SYSTEM_DEFAULT_GREETING_PATH


@router.get("/api/greeting/files/{filename}/audio")
def stream_file_audio(filename: str, request: Request, session: dict = Depends(get_session)):
    return _stream_wav_response(_resolve_file_path(session, filename), request)


@router.get("/api/greeting/default/audio")
def stream_default_audio(request: Request, session: dict = Depends(get_session)):
    return _stream_wav_response(_resolve_default_path(), request)


def _call_path(session: dict, path) -> dict:
    try:
        label = phone_service.call_extension(str(path), session["extension"])
    except PhoneServiceUnavailable as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.exception("Failed to place phone-playback call for greeting (ext %s)", session["extension"])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not place the call: {e}")
    return {"ok": True, "extension": session["extension"], "call_id": label}


@router.post("/api/greeting/files/{filename}/call")
def call_to_review_file(filename: str, session: dict = Depends(get_session)):
    """Rings the logged-in user's own extension and plays this specific
    greeting file down the line -- always dialing the caller's own
    extension, same as POST /api/messages/{id}/call, so this can't ring
    anyone else's desk phone."""
    return _call_path(session, _resolve_file_path(session, filename))


@router.post("/api/greeting/default/call")
def call_to_review_default(session: dict = Depends(get_session)):
    return _call_path(session, _resolve_default_path())


@router.post("/api/greeting/files/{filename}/activate")
def activate_file(filename: str, session: dict = Depends(get_session)):
    """Sets `filename` as the extension's active greeting -- the file
    manager's "Select" action, backed by 3CX's own xapi (see
    greeting_service.set_active)."""
    try:
        greeting_service.set_active(session["extension"], filename)
    except greeting_service.GreetingActionError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    return {"ok": True, "active_filename": filename}


@router.post("/api/greeting/default/activate")
def activate_default(session: dict = Depends(get_session)):
    try:
        greeting_service.set_active(session["extension"], None)
    except greeting_service.GreetingActionError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    return {"ok": True, "active_filename": None}


@router.delete("/api/greeting/files/{filename}")
def delete_file(filename: str, session: dict = Depends(get_session)):
    """Permanently deletes one greeting file -- 3CX's system default (no
    per-extension file) isn't reachable through this route at all, so
    there's nothing here that could delete it."""
    try:
        greeting_service.delete_file(session["extension"], filename)
    except greeting_service.GreetingActionError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    return {"ok": True}


@router.post("/api/greeting/record")
def start_greeting_recording(session: dict = Depends(get_session)):
    """Kicks off 3CX's own phone-based recording flow -- see
    greeting_service.start_recording. Doesn't wait for the call or the
    recording, and doesn't activate the result either: a fresh recording
    just becomes a new entry in the file list, same as 3CX's own webclient.
    The Settings modal polls GET /api/greeting/record/status while it's in
    progress, then refreshes the file list once it lands."""
    try:
        filename = greeting_service.start_recording(session["extension"])
    except greeting_service.GreetingActionError as e:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    return {"ok": True, "filename": filename, "status": "waiting"}


@router.get("/api/greeting/record/status")
def greeting_recording_status(session: dict = Depends(get_session)):
    """Polled by the Settings modal while a recording call is in progress."""
    return greeting_service.recording_status(session["extension"])


# --- Voicemail PIN (Settings > Change Phone PIN) -----------------------------
# Same write path as the Yealink phone's own "Change PIN" screen
# (routes/yealink.py's pin_set) -- both call threecx_db.set_pin directly,
# there's no native RPC to impersonate here (see set_pin's docstring).

class SetPinRequest(BaseModel):
    pin: str


@router.get("/api/pin")
def get_pin(session: dict = Depends(get_session)):
    """Always the logged-in user's own PIN -- same self-service scoping as
    GET /api/greeting above."""
    return {"pin": threecx_db.get_pin(session["extension"])}


@router.post("/api/pin")
def set_pin(body: SetPinRequest, session: dict = Depends(get_session)):
    pin = body.pin
    if not pin.isdigit():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="PIN must be numeric")
    if not (MIN_PIN_LEN <= len(pin) <= MAX_PIN_LEN):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"PIN must be {MIN_PIN_LEN}-{MAX_PIN_LEN} digits",
        )
    extension = session["extension"]
    if not threecx_db.set_pin(extension, pin):
        logger.error("Web PIN change: no voicemail row updated for extension %s", extension)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not update PIN")
    return {"ok": True}
