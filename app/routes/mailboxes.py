from fastapi import APIRouter, Depends, HTTPException, status

from .. import access, app_db, threecx_db
from ..deps import get_session

router = APIRouter()


def _is_unheard(heard) -> bool:
    return heard is None or heard in ("", "0")


def _first_reviewer(reviewers: list[dict]):
    """Earliest (by reviewed_at) reviewer among a message's merged reviewer
    list, or None if nobody has reviewed it yet. Distinct from
    access.merge_review_status's own ordering, which sorts reviewed viewers
    most-recent-first for display in the detail panel."""
    reviewed = [r for r in reviewers if r["reviewed_at"]]
    if not reviewed:
        return None
    first = min(reviewed, key=lambda r: r["reviewed_at"])
    return {"extension": first["extension"], "name": first["name"], "reviewed_at": first["reviewed_at"]}


def _format_messages(extension: str) -> list[dict]:
    messages = threecx_db.messages_for_mailbox(extension)
    reviews_by_message = app_db.reviews_for_mailbox(extension)
    words_by_message = app_db.transcription_words_for_messages([m["id"] for m in messages])
    viewers = access.viewers_for_mailbox(extension)
    result = []
    for m in messages:
        reviewers = access.merge_review_status(
            viewers,
            reviews_by_message.get(m["id"], []),
            mailbox_extension=extension,
            native_heard_at=m["heard_time"] if not _is_unheard(m["heard"]) else None,
        )
        result.append(
            {
                "id": m["id"],
                "caller": m["caller"],
                "caller_name": m["caller_name"],
                "crm_name": threecx_db.crm_display_name(m["crm_contact"]),
                "callee": m["callee"],
                "duration": m["duration"],
                "created_time": m["created_time"],
                "heard": not _is_unheard(m["heard"]),
                "heard_time": m["heard_time"],
                "forwarded_by": m["forwarded_by"],
                "forwarded_to": m["forwarded_to"],
                "transcription": m["transcription"],
                "transcription_words": words_by_message.get(m["id"], []),
                "reviewers": reviewers,
                "first_reviewer": _first_reviewer(reviewers),
            }
        )
    return result


@router.get("/api/mailboxes")
def list_mailboxes(session: dict = Depends(get_session)):
    boxes = access.accessible_mailboxes(session)
    result = []
    for box in boxes:
        messages = threecx_db.messages_for_mailbox(box["extension"])
        unread = sum(1 for m in messages if _is_unheard(m["heard"]))
        can_delete = access.can_delete_from_mailbox(session, box["extension"])
        result.append({**box, "unread": unread, "total": len(messages), "can_delete": can_delete})
    return result


@router.get("/api/mailboxes/all/messages")
def all_mailbox_messages(session: dict = Depends(get_session)):
    """Aggregate messages across every mailbox this session can access,
    each tagged with which mailbox it belongs to, sorted newest first."""
    boxes = access.accessible_mailboxes(session)
    combined = []
    for box in boxes:
        for m in _format_messages(box["extension"]):
            combined.append({**m, "mailbox_extension": box["extension"], "mailbox_name": box["name"]})
    combined.sort(key=lambda m: m["created_time"] or "", reverse=True)
    return combined


@router.get("/api/mailboxes/{extension}/messages")
def mailbox_messages(extension: str, session: dict = Depends(get_session)):
    if not access.can_access_mailbox(session, extension):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No access to this mailbox")
    return _format_messages(extension)
