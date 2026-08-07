import logging

from fastapi import APIRouter, Depends, HTTPException, status

from .. import access, threecx_db
from ..deps import get_session
from ..phone_service import PhoneServiceUnavailable, phone_service

logger = logging.getLogger(__name__)

router = APIRouter()


def _check_access_or_403(extension: str, session: dict):
    if not access.can_access_mailbox(session, extension):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this mailbox")
    if extension in access.group_mailbox_extensions():
        # Group mailboxes aren't SIP users -- they have rows in
        # myphone_callhistory_v14 (calls get logged against them as they're
        # routed through), but the Call Log view is deliberately never
        # exposed for them, in the API as well as the UI.
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Group mailboxes have no call log")


def _format_call(row: dict) -> dict:
    status_by_calltype = {1: "missed", 2: "received", 3: "outgoing"}
    call_status = status_by_calltype.get(row["calltype"], "unknown")
    is_external = bool(row["party_dntype"] and (row["party_dntype"] & 128) == 128)
    counterpart_key = row["party_callerid"] if is_external else row["party_dn"]
    return {
        "id": row["idmpch14"],
        "calltype": row["calltype"],
        "status": call_status,
        "direction": "out" if call_status == "outgoing" else "in",
        "party_name": row["party_name"],
        "party_dn": row["party_dn"],
        "party_callerid": row["party_callerid"],
        "is_external": is_external,
        "counterpart_key": counterpart_key,
        "start_time": row["start_time"],
        "established_time": row["established_time"],
        "end_time": row["end_time"],
    }


@router.get("/api/mailboxes/{extension}/calls")
def mailbox_calls(extension: str, session: dict = Depends(get_session)):
    _check_access_or_403(extension, session)
    return [_format_call(row) for row in threecx_db.call_history(extension)]


@router.get("/api/mailboxes/{extension}/calls/{counterpart_key}")
def mailbox_calls_with_counterpart(extension: str, counterpart_key: str, session: dict = Depends(get_session)):
    _check_access_or_403(extension, session)
    return [_format_call(row) for row in threecx_db.call_history_with_counterpart(extension, counterpart_key)]


@router.post("/api/mailboxes/{extension}/calls/{counterpart_key}/call")
def call_counterpart(extension: str, counterpart_key: str, session: dict = Depends(get_session)):
    """Click-to-call from the counterpart-history panel: rings the logged-in
    user's own extension, then blind-transfers to counterpart_key once
    answered. Same phone_service primitive and error handling as
    messages.py's /callback route. counterpart_key is already a dialable
    number/extension (see _format_call: party_callerid for external rows,
    party_dn for internal ones), so no extra lookup is needed."""
    _check_access_or_403(extension, session)
    try:
        label = phone_service.call_back(session["extension"], counterpart_key)
    except PhoneServiceUnavailable as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(e))
    except Exception as e:
        logger.exception("Failed to place call-back to %s (ext %s)", counterpart_key, session["extension"])
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Could not place the call: {e}")
    return {"ok": True, "extension": session["extension"], "dest": counterpart_key, "call_id": label}
