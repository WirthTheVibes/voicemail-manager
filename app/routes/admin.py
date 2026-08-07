from collections import defaultdict

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .. import app_db, config, threecx_db
from ..deps import require_admin

router = APIRouter()


@router.get("/api/admin/extensions")
def list_extensions(session: dict = Depends(require_admin)):
    hidden = app_db.hidden_extensions()
    return [{**row, "hidden": row["extension"] in hidden} for row in threecx_db.directory()]


class HiddenRequest(BaseModel):
    hidden: bool


@router.put("/api/admin/users/{extension}/hidden")
def set_user_hidden(extension: str, body: HiddenRequest, session: dict = Depends(require_admin)):
    app_db.set_hidden(extension, body.hidden)
    return {"extension": extension, "hidden": body.hidden}


# --- Users tab: explicit per-user mailbox grants ------------------------------
@router.get("/api/admin/users/{extension}/grants")
def get_user_grants(extension: str, session: dict = Depends(require_admin)):
    return {"extension": extension, "mailboxes": app_db.grants_with_status_for_viewer(extension)}


class GrantItem(BaseModel):
    mailbox_extension: str
    hide_review_status: bool = False
    mwi_suppress: bool = False
    notify_suppress: bool = False


class GrantsRequest(BaseModel):
    mailboxes: list[GrantItem] = []


@router.put("/api/admin/users/{extension}/grants")
def set_user_grants(extension: str, body: GrantsRequest, session: dict = Depends(require_admin)):
    grants = [g.model_dump() for g in body.mailboxes if g.mailbox_extension != extension]
    app_db.set_grants_for_viewer(extension, grants)
    return {"extension": extension, "mailboxes": grants}


# --- Departments tab: department mailboxes + membership -----------------------
@router.get("/api/admin/departments")
def list_departments(session: dict = Depends(require_admin)):
    directory = threecx_db.directory()
    by_department = defaultdict(list)
    for row in directory:
        dept = row["department"] or "(no department)"
        by_department[dept].append(row)

    dept_mailboxes = app_db.all_department_mailboxes()
    result = []
    for dept, extensions in sorted(by_department.items()):
        result.append(
            {
                "department": dept,
                "extensions": extensions,
                "mailboxes": dept_mailboxes.get(dept, []),
            }
        )
    return result


class DepartmentMailboxesRequest(BaseModel):
    mailboxes: list[str] = []


@router.put("/api/admin/departments/{department}/mailboxes")
def set_department_mailboxes(
    department: str, body: DepartmentMailboxesRequest, session: dict = Depends(require_admin)
):
    app_db.set_department_mailboxes(department, body.mailboxes)
    return {"department": department, "mailboxes": body.mailboxes}


@router.get("/api/admin/departments/{department}/mailboxes/{mailbox_extension}/members")
def get_department_mailbox_members(
    department: str, mailbox_extension: str, session: dict = Depends(require_admin)
):
    dept_members = {row["extension"] for row in threecx_db.department_extensions(department)}
    excluded = set(app_db.excluded_members(department, mailbox_extension))
    extra = set(app_db.viewers_granted_for_mailbox(mailbox_extension)) - dept_members
    hidden_review_members = set(app_db.hidden_review_members(department, mailbox_extension))
    mwi_suppressed_members = set(app_db.mwi_suppressed_members(department, mailbox_extension))
    notify_suppressed_members = set(app_db.notify_suppressed_members(department, mailbox_extension))
    hidden_review_extra = app_db.hidden_review_viewers_for_mailbox(mailbox_extension)
    mwi_suppressed_extra = app_db.mwi_suppressed_viewers_for_mailbox(mailbox_extension)
    notify_suppressed_extra = app_db.notify_suppressed_viewers_for_mailbox(mailbox_extension)
    return {
        "implicit": [
            {
                "extension": e,
                "hide_review_status": e in hidden_review_members,
                "mwi_suppress": e in mwi_suppressed_members,
                "notify_suppress": e in notify_suppressed_members,
            }
            for e in sorted(dept_members - excluded)
        ],
        "excluded": sorted(excluded),
        "extra": [
            {
                "extension": e,
                "hide_review_status": e in hidden_review_extra,
                "mwi_suppress": e in mwi_suppressed_extra,
                "notify_suppress": e in notify_suppressed_extra,
            }
            for e in sorted(extra)
        ],
    }


class DepartmentMemberStatus(BaseModel):
    extension: str
    hide_review_status: bool = False
    mwi_suppress: bool = False
    notify_suppress: bool = False


class DepartmentMailboxMembersRequest(BaseModel):
    excluded: list[str] = []
    extra: list[DepartmentMemberStatus] = []
    hidden_review_members: list[str] = []
    mwi_suppressed_members: list[str] = []
    notify_suppressed_members: list[str] = []


@router.put("/api/admin/departments/{department}/mailboxes/{mailbox_extension}/members")
def set_department_mailbox_members(
    department: str,
    mailbox_extension: str,
    body: DepartmentMailboxMembersRequest,
    session: dict = Depends(require_admin),
):
    app_db.set_excluded_members(department, mailbox_extension, body.excluded)

    dept_members = {row["extension"] for row in threecx_db.department_extensions(department)}
    implicit_members = dept_members - set(body.excluded)
    app_db.set_hidden_review_members(
        department, mailbox_extension, [m for m in body.hidden_review_members if m in implicit_members]
    )
    app_db.set_mwi_suppressed_members(
        department, mailbox_extension, [m for m in body.mwi_suppressed_members if m in implicit_members]
    )
    app_db.set_notify_suppressed_members(
        department, mailbox_extension, [m for m in body.notify_suppressed_members if m in implicit_members]
    )

    current_extra = set(app_db.viewers_granted_for_mailbox(mailbox_extension)) - dept_members
    new_extra_status = {
        v.extension: (v.hide_review_status, v.mwi_suppress, v.notify_suppress) for v in body.extra
    }
    new_extra = set(new_extra_status)
    for ext in current_extra - new_extra:
        app_db.remove_grant(ext, mailbox_extension)
    for ext, (hide_review_status, mwi_suppress, notify_suppress) in new_extra_status.items():
        app_db.add_grant(ext, mailbox_extension, hide_review_status, mwi_suppress, notify_suppress)

    return {
        "implicit": [
            {
                "extension": e,
                "hide_review_status": e in set(body.hidden_review_members),
                "mwi_suppress": e in set(body.mwi_suppressed_members),
                "notify_suppress": e in set(body.notify_suppressed_members),
            }
            for e in sorted(implicit_members)
        ],
        "excluded": sorted(body.excluded),
        "extra": [
            {
                "extension": e,
                "hide_review_status": new_extra_status[e][0],
                "mwi_suppress": new_extra_status[e][1],
                "notify_suppress": new_extra_status[e][2],
            }
            for e in sorted(new_extra)
        ],
    }


# --- Settings > Transcription tab ---------------------------------------------
def _transcription_settings_response(settings: dict) -> dict:
    return {**settings, "openai_available": bool(config.OPENAI_API_KEY)}


@router.get("/api/admin/transcription-settings")
def get_transcription_settings(session: dict = Depends(require_admin)):
    return _transcription_settings_response(app_db.get_transcription_settings())


class TranscriptionSettingsRequest(BaseModel):
    enabled: bool
    engine: str


@router.put("/api/admin/transcription-settings")
def set_transcription_settings(body: TranscriptionSettingsRequest, session: dict = Depends(require_admin)):
    if body.engine not in ("local", "openai"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="engine must be 'local' or 'openai'")
    if body.engine == "openai" and not config.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="OpenAI transcription is not configured (OPENAI_API_KEY not set in .env)",
        )
    app_db.set_transcription_settings(body.enabled, body.engine)
    return _transcription_settings_response(app_db.get_transcription_settings())


# --- Settings > Notifications tab ----------------------------------------------
def _notification_settings_response(settings: dict) -> dict:
    return {**settings, "smtp_configured": config.SMTP_CONFIGURED, "pwa_configured": config.PUSH_CONFIGURED}


@router.get("/api/admin/notification-settings")
def get_notification_settings(session: dict = Depends(require_admin)):
    return _notification_settings_response(app_db.get_notification_settings())


class NotificationSettingsRequest(BaseModel):
    smtp_enabled: bool
    pwa_enabled: bool


@router.put("/api/admin/notification-settings")
def set_notification_settings(body: NotificationSettingsRequest, session: dict = Depends(require_admin)):
    if body.smtp_enabled and not config.SMTP_CONFIGURED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="SMTP is not configured (SMTP_HOST/SMTP_FROM not set in .env)",
        )
    if body.pwa_enabled and not config.PUSH_CONFIGURED:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Push notifications are not configured (VAPID keys not set in .env)",
        )
    app_db.set_notification_settings(body.smtp_enabled, body.pwa_enabled)
    return _notification_settings_response(app_db.get_notification_settings())
