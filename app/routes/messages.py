import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .. import access, app_db, config, threecx_db, threecx_notify
from ..deps import get_session
from ..events import broadcaster
from ..phone_service import PhoneServiceUnavailable, phone_service
from ..transcription import TranscriptionError, transcribe

logger = logging.getLogger(__name__)

router = APIRouter()

CHUNK_SIZE = 1024 * 256


def _get_message_or_403(message_id: int, session: dict):
    message = threecx_db.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Message not found")
    if not access.can_access_mailbox(session, message["callee"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this message")
    return message


def _resolve_audio_path(message: dict) -> Path:
    try:
        candidate = config.resolve_voicemail_path(message["callee"], message["wav_file"])
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid audio path")
    if not candidate.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Audio file not found")
    return candidate


@router.get("/api/messages/{message_id}/audio")
def stream_audio(message_id: int, request: Request, session: dict = Depends(get_session)):
    message = _get_message_or_403(message_id, session)
    path = _resolve_audio_path(message)
    file_size = path.stat().st_size

    range_header = request.headers.get("range")
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


def _mark_heard_and_reviewed_after_call(message_id: int, callee: str, reviewer: str, answered: bool, played_to_end: bool) -> None:
    """`on_complete` for the phone-icon playback -- fires on pjsua2's own
    worker thread once the call disconnects. Only counts as a real listen
    if the person actually picked up AND the WAV played to completion (not
    just "a call was placed"); hanging up partway through doesn't count.
    app_db/threecx_notify are plain blocking I/O with no thread affinity,
    so this is safe to run straight off the pjsua2 thread -- see set_heard
    and mark_reviewed above, which this mirrors."""
    if not (answered and played_to_end):
        return
    try:
        threecx_notify.notify_heard(message_id, callee, True)
    except threecx_notify.NotifyError:
        logger.exception("Phone-playback: mark-heard failed for message %s", message_id)
    app_db.record_heard_audit(message_id, reviewer, True)
    app_db.mark_reviewed(message_id, callee, reviewer)
    viewers = access.viewers_for_mailbox(callee)
    reviews = app_db.get_reviews(message_id)
    fresh_message = threecx_db.get_message(message_id)
    reviewers = access.merge_review_status(
        viewers, reviews, mailbox_extension=callee,
        native_heard_at=_native_heard_at(fresh_message) if fresh_message else None,
    )
    broadcaster.publish({"type": "reviewed", "mailbox": callee, "message_id": message_id, "reviewers": reviewers})


@router.post("/api/messages/{message_id}/call")
def call_to_my_extension(message_id: int, session: dict = Depends(get_session)):
    """Places a SIP call to the caller's own extension and plays this
    message's WAV down the line once answered -- the phone-icon side of the
    detail panel's speaker/phone playback toggle. Always dials the logged-in
    user's own extension (never an arbitrary one from the request body), so
    this can't be used to ring someone else's desk phone."""
    message = _get_message_or_403(message_id, session)
    path = _resolve_audio_path(message)
    on_complete = lambda answered, played_to_end: _mark_heard_and_reviewed_after_call(
        message_id, message["callee"], session["extension"], answered, played_to_end
    )
    try:
        label = phone_service.call_extension(str(path), session["extension"], on_complete=on_complete)
    except PhoneServiceUnavailable as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.exception("Failed to place phone-playback call for message %s (ext %s)", message_id, session["extension"])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not place the call: {e}")
    return {"ok": True, "extension": session["extension"], "call_id": label}


@router.post("/api/messages/{message_id}/callback")
def call_back(message_id: int, session: dict = Depends(get_session)):
    """"Call back" button: rings the logged-in user's own extension, then
    blind-transfers that call to the number that left this voicemail. Like
    /call above, always rings the logged-in user's own extension -- never an
    arbitrary one from the request body -- so this can't be used to ring
    someone else's desk phone and hand it off to a number of the caller's
    choosing."""
    message = _get_message_or_403(message_id, session)
    if not message["caller"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This message has no caller number to call back")
    try:
        label = phone_service.call_back(session["extension"], message["caller"])
    except PhoneServiceUnavailable as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.exception("Failed to place call-back for message %s (ext %s)", message_id, session["extension"])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not place the call: {e}")
    return {"ok": True, "extension": session["extension"], "caller": message["caller"], "call_id": label}


class HeardRequest(BaseModel):
    heard: bool


@router.post("/api/messages/{message_id}/heard")
def set_heard(message_id: int, body: HeardRequest, session: dict = Depends(get_session)):
    message = _get_message_or_403(message_id, session)
    if body.heard and access.is_hidden_reviewer_for(session["extension"], message["callee"]):
        # "heard" is a single mailbox-wide bit (unlike the per-viewer review
        # row), so letting a hidden reviewer flip it would tip off the
        # mailbox owner that someone reviewed the message -- exactly what
        # hide-review-status is meant to keep invisible. Still audited, but
        # the real 3CX flag (and its SIP NOTIFY) is left untouched.
        app_db.record_heard_audit(message_id, session["extension"], body.heard)
        return {"ok": True, "heard": message["heard"]}
    # The native RPC is the only write path (see threecx_notify.py) — it's
    # what makes 3CX itself update s_voicemail *and* fire the SIP NOTIFY to
    # the phone. A raw SQL UPDATE here would flip the row without 3CX ever
    # recognizing the change, so there is no DB-write fallback: if the RPC
    # fails, the request fails rather than reporting a silent partial state.
    #
    # Impersonate the mailbox owner (callee), not the viewer — the RPC is
    # ownership-scoped and no-ops against any other extension, which matters
    # here since a delegate/supervisor can mark someone else's mailbox
    # message heard.
    try:
        threecx_notify.notify_heard(message_id, message["callee"], body.heard)
    except threecx_notify.NotifyError:
        logger.exception("Native 3CX notify failed for message %s (ext %s)", message_id, message["callee"])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not update voicemail status in 3CX",
        )
    app_db.record_heard_audit(message_id, session["extension"], body.heard)
    broadcaster.publish({"type": "heard", "mailbox": message["callee"], "message_id": message_id, "heard": body.heard})
    return {"ok": True, "heard": body.heard}


@router.delete("/api/messages/{message_id}")
def delete_message(message_id: int, session: dict = Depends(get_session)):
    """Hard-deletes the voicemail from 3CX itself (s_voicemail row is gone,
    not just flagged) via the same native RPC path as set_heard -- see
    threecx_notify.notify_delete. No DB-write fallback, for the same reason
    set_heard has none: a raw SQL delete here would desync 3CX's own state
    (MWI counts, webclient listing) from this app's view of it.

    For a group mailbox, deleting is further restricted to Supervisor/
    Manager/System Owner -- see access.can_delete_from_mailbox."""
    message = _get_message_or_403(message_id, session)
    if not access.can_delete_from_mailbox(session, message["callee"]):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a Supervisor or Manager can delete voicemails from this mailbox",
        )
    try:
        threecx_notify.notify_delete(message_id, message["callee"])
    except threecx_notify.NotifyError:
        logger.exception("Native 3CX delete failed for message %s (ext %s)", message_id, message["callee"])
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not delete voicemail in 3CX",
        )
    broadcaster.publish({"type": "deleted", "mailbox": message["callee"], "message_id": message_id})
    return {"ok": True}


@router.post("/api/messages/{message_id}/transcribe")
def transcribe_message(message_id: int, session: dict = Depends(get_session)):
    """Manual "Generate transcription" button in the detail panel — runs
    against whichever engine is currently selected (Settings > Transcription
    tab), bypassing the background worker's per-run failed-message skip
    (transcription_worker.py) since this is an explicit retry."""
    message = _get_message_or_403(message_id, session)
    if not app_db.get_transcription_settings()["enabled"]:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Transcription is disabled")
    path = _resolve_audio_path(message)
    try:
        result = transcribe(str(path))
    except TranscriptionError as e:
        logger.exception("Manual transcription failed for message %s", message_id)
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(e))
    threecx_db.set_transcription(message_id, result["text"])
    if result["words"]:
        app_db.set_transcription_words(message_id, result["words"])
    return {"ok": True, "transcription": result["text"], "words": result["words"]}


def _native_heard_at(message: dict):
    """message["heard_time"] if the message is actually marked heard, else
    None -- see access.merge_review_status's native_heard_at fallback."""
    if message["heard"] in (None, "", "0"):
        return None
    return message["heard_time"]


@router.post("/api/messages/{message_id}/reviewed")
def mark_reviewed(message_id: int, session: dict = Depends(get_session)):
    message = _get_message_or_403(message_id, session)
    app_db.mark_reviewed(message_id, message["callee"], session["extension"])
    viewers = access.viewers_for_mailbox(message["callee"])
    reviews = app_db.get_reviews(message_id)
    reviewers = access.merge_review_status(
        viewers, reviews, mailbox_extension=message["callee"], native_heard_at=_native_heard_at(message)
    )
    broadcaster.publish({"type": "reviewed", "mailbox": message["callee"], "message_id": message_id, "reviewers": reviewers})
    return {"ok": True, "reviewers": reviewers}


@router.get("/api/messages/{message_id}/callbacks")
def message_callbacks(message_id: int, session: dict = Depends(get_session)):
    """Per-reviewer "called back" signal for the detail panel's review list:
    whether each reviewer placed an outgoing call to the caller after their
    own reviewed_at. Kept as its own on-demand endpoint (called only for the
    open message) rather than folded into the bulk message list -- see
    access.called_back_after_review."""
    message = _get_message_or_403(message_id, session)
    viewers = access.viewers_for_mailbox(message["callee"])
    reviews = app_db.get_reviews(message_id)
    reviewers = access.merge_review_status(
        viewers, reviews, mailbox_extension=message["callee"], native_heard_at=_native_heard_at(message)
    )
    return {"callbacks": access.called_back_after_review(reviewers, message["caller"])}


@router.get("/api/messages/{message_id}/call-path")
def call_path(message_id: int, session: dict = Depends(get_session)):
    """Sequence of extensions/queues/scripts 3CX actually routed the call
    through before it landed in this mailbox -- see threecx_db.call_path for
    how the chain is reconstructed (and why the trunk DN is swapped for the
    actual calling number as the origin)."""
    _get_message_or_403(message_id, session)
    return threecx_db.call_path(message_id)


@router.delete("/api/messages/{message_id}/reviewed")
def unmark_reviewed(message_id: int, session: dict = Depends(get_session)):
    """Removes the caller's own review row. Debug/testing counterpart to
    mark_reviewed (Ctrl+click in the UI) -- lets the reviewed-tracking
    system be exercised both ways without editing app_db by hand."""
    message = _get_message_or_403(message_id, session)
    app_db.remove_review(message_id, session["extension"])
    viewers = access.viewers_for_mailbox(message["callee"])
    reviews = app_db.get_reviews(message_id)
    reviewers = access.merge_review_status(
        viewers, reviews, mailbox_extension=message["callee"], native_heard_at=_native_heard_at(message)
    )
    broadcaster.publish({"type": "reviewed", "mailbox": message["callee"], "message_id": message_id, "reviewers": reviewers})
    return {"ok": True, "reviewers": reviewers}
