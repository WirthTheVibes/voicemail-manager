"""
Access model: for a given session, determine which 3CX mailboxes the user
may view. Union of: own extension, explicit admin-granted mailboxes (Users
tab), department mailboxes implicitly shared with the user's own department
(Departments tab, minus explicit exclusions), Supervisor department
auto-access, and Manager/System Owner org-wide auto-access. All read-only
against 3CX (Q2/Q6/Q7) and the app's SQLite DB.
"""
from datetime import datetime, timezone

from . import app_db, auth, threecx_db


def _full_name(firstname, lastname) -> str:
    return " ".join(part for part in (firstname, lastname) if part).strip()


def current_identity(extension: str) -> dict:
    """Live-refreshed identity/display fields for an authenticated extension.

    3CX is the source of truth here — name, department, and role can all
    change mid-session (e.g. an admin renaming an extension or changing its
    role), so these are never trusted from a cached session token. Reuses
    only the already-approved Q2/Q6 queries; no new SQL statements.
    """
    directory_row = next(
        (r for r in threecx_db.directory() if r["extension"] == extension), None
    )
    dept_role = threecx_db.department_and_role(extension)
    role_flags = auth.derive_role_flags(dept_role["roletag"] if dept_role else None)
    return {
        "firstname": directory_row["firstname"] if directory_row else None,
        "lastname": directory_row["lastname"] if directory_row else None,
        "department": dept_role["department"] if dept_role else None,
        **role_flags,
    }


def group_mailbox_extensions() -> set[str]:
    """Every extension designated as some department's shared mailbox
    (Departments tab), regardless of which department owns it."""
    return {ext for mailboxes in app_db.all_department_mailboxes().values() for ext in mailboxes}


def notification_recipients_for_mailbox(mailbox_extension: str) -> list[str]:
    """Extensions who should be notified of a new voicemail in this mailbox.

    For a personal mailbox that's just its owner. For a group mailbox
    (Departments tab) it's every member of the department(s) it's assigned
    to as the shared mailbox (minus that department's explicit exclusions),
    plus anyone with an explicit per-user grant (Users tab) -- deliberately
    NOT filtered the way viewers_for_mailbox() filters out admins/
    managers/supervisors/hidden-review viewers, since that filtering exists
    to hide *review status* from the mailbox owner, not to decide who
    should be emailed about a new message.

    A member/viewer with notify_suppress set (Settings > Users or
    Departments, "Suppress notifications") opted out specifically of the
    notification -- they still see the mailbox, still light up their phone
    (unless mwi_suppress is also set), just don't get emailed/pushed.
    """
    if mailbox_extension not in group_mailbox_extensions():
        return [mailbox_extension]

    recipients: set[str] = set()
    dept_mailboxes = app_db.all_department_mailboxes()
    for department, mailboxes in dept_mailboxes.items():
        if mailbox_extension not in mailboxes:
            continue
        excluded = set(app_db.excluded_members(department, mailbox_extension))
        notify_suppressed = set(app_db.notify_suppressed_members(department, mailbox_extension))
        for row in threecx_db.department_extensions(department):
            ext = row["extension"]
            if ext != mailbox_extension and ext not in excluded and ext not in notify_suppressed:
                recipients.add(ext)

    notify_suppressed_extra = app_db.notify_suppressed_viewers_for_mailbox(mailbox_extension)
    for viewer_ext in app_db.viewers_granted_for_mailbox(mailbox_extension):
        if viewer_ext not in notify_suppressed_extra:
            recipients.add(viewer_ext)
    return sorted(recipients)


def accessible_mailboxes(session: dict) -> list[dict]:
    """Returns a de-duplicated list of {extension, name, source, department,
    is_group} the user may view. An extension flagged "hidden" (Users tab
    checkbox) is dropped entirely UNLESS it's a group mailbox — hiding a
    group mailbox individually is meaningless since its whole purpose is to
    be reachable through the Group Mailboxes section instead."""
    extension = session["extension"]
    seen: dict[str, dict] = {}
    directory_by_ext = {row["extension"]: row for row in threecx_db.directory()}
    group_exts = group_mailbox_extensions()
    hidden_exts = app_db.hidden_extensions()

    def add(ext: str, name: str, source: str):
        if ext not in seen:
            if ext in hidden_exts and ext not in group_exts:
                return
            row = directory_by_ext.get(ext)
            seen[ext] = {
                "extension": ext,
                "name": name,
                "source": source,
                "department": (row["department"] if row else None) or "",
                "is_group": ext in group_exts,
            }

    def name_for(ext: str) -> str:
        row = directory_by_ext.get(ext)
        return _full_name(row["firstname"], row["lastname"]) if row else ext

    add(extension, _full_name(session.get("firstname"), session.get("lastname")), "personal")

    for mailbox_ext in app_db.grants_for_viewer(extension):
        add(mailbox_ext, name_for(mailbox_ext), "granted")

    department = session.get("department")
    if department:
        excluded_by_mailbox = {
            mailbox_ext: set(app_db.excluded_members(department, mailbox_ext))
            for mailbox_ext in app_db.department_mailboxes(department)
        }
        for mailbox_ext, excluded in excluded_by_mailbox.items():
            if extension not in excluded:
                add(mailbox_ext, name_for(mailbox_ext), f"department:{department}")

    if session.get("is_manager") or session.get("is_system_owner"):
        for row in directory_by_ext.values():
            add(row["extension"], _full_name(row["firstname"], row["lastname"]), "org-wide")
    elif session.get("is_supervisor") and department:
        for row in threecx_db.department_extensions(department):
            add(row["extension"], _full_name(row["firstname"], row["lastname"]), "supervisor-department")

    return list(seen.values())


def can_access_mailbox(session: dict, extension: str) -> bool:
    return any(m["extension"] == extension for m in accessible_mailboxes(session))


def can_delete_from_mailbox(session: dict, mailbox_extension: str) -> bool:
    """Deleting a voicemail is destructive and irreversible (native 3CX hard
    delete, see threecx_notify.notify_delete) -- for a group mailbox
    (Departments tab, shared by a whole team) that's restricted to
    Supervisor/Manager/System Owner role holders, even though
    can_access_mailbox lets every department member view and mark-heard/
    reviewed it. Personal mailboxes and individually-granted ones (Users
    tab) are unrestricted: the owner, and anyone explicitly delegated
    access to their own mail, can always delete it. Caller must already
    have checked can_access_mailbox -- this only narrows the group case."""
    if mailbox_extension not in group_mailbox_extensions():
        return True
    return bool(session.get("is_supervisor") or session.get("is_manager") or session.get("is_system_owner"))


def viewers_for_mailbox(mailbox_extension: str) -> list[dict]:
    """Returns [{extension, name}] for everyone whose accessible_mailboxes()
    would include mailbox_extension — the inverse of that function, used to
    show a mailbox's expected reviewers. Mirrors its five access rules
    exactly (personal, granted, department-implicit, supervisor, org-wide)
    but computed mailbox-first with bulk queries instead of one
    accessible_mailboxes() call per directory entry.

    App admins and anyone with the Manager/Supervisor/System Owner role are
    excluded from the returned list (unless it's their own mailbox) — they
    can still access and review a mailbox via accessible_mailboxes(), but
    the mailbox owner isn't shown that oversight is happening. A per-grant
    "hide review status" flag (Users tab) does the same for an individual
    granted viewer, and the Departments tab equivalent does it for an
    individual implicit department member.
    """
    directory_by_ext = {row["extension"]: row for row in threecx_db.directory()}
    roles_by_ext = {row["extension"]: row for row in threecx_db.all_roles()}
    dept_mailboxes = app_db.all_department_mailboxes()
    admin_extensions = app_db.admin_extensions()
    hidden_review_viewers = app_db.hidden_review_viewers_for_mailbox(mailbox_extension)
    hidden_review_dept_members = {
        member
        for department, mailboxes in dept_mailboxes.items()
        if mailbox_extension in mailboxes
        for member in app_db.hidden_review_members(department, mailbox_extension)
    }

    seen: dict[str, dict] = {}

    def add(ext: str):
        if ext in directory_by_ext and ext not in seen:
            row = directory_by_ext[ext]
            seen[ext] = {"extension": ext, "name": _full_name(row["firstname"], row["lastname"])}

    # personal: the mailbox owner always sees their own mailbox — but a group
    # mailbox (Departments tab) isn't a person, so it doesn't get to "review"
    # its own messages just by being the callee
    if mailbox_extension not in group_mailbox_extensions():
        add(mailbox_extension)

    # granted: explicit per-user grants (Users tab)
    for viewer_ext in app_db.viewers_granted_for_mailbox(mailbox_extension):
        add(viewer_ext)

    # department-implicit: members of any department this extension is
    # designated as a shared mailbox for (Departments tab), minus exclusions
    for department, mailboxes in dept_mailboxes.items():
        if mailbox_extension not in mailboxes:
            continue
        excluded = set(app_db.excluded_members(department, mailbox_extension))
        for row in threecx_db.department_extensions(department):
            if row["extension"] != mailbox_extension and row["extension"] not in excluded:
                add(row["extension"])

    # supervisor: a supervisor auto-sees the personal mailbox of every
    # member of their own department
    owner_department = roles_by_ext.get(mailbox_extension, {}).get("department")
    if owner_department:
        for ext, role in roles_by_ext.items():
            if role["department"] == owner_department and auth.derive_role_flags(role["roletag"])["is_supervisor"]:
                add(ext)

    # org-wide: managers / system owners see every mailbox
    for ext, role in roles_by_ext.items():
        flags = auth.derive_role_flags(role["roletag"])
        if flags["is_manager"] or flags["is_system_owner"]:
            add(ext)

    def is_hidden_reviewer(ext: str) -> bool:
        if ext == mailbox_extension:
            return False
        if ext in admin_extensions:
            return True
        if ext in hidden_review_viewers or ext in hidden_review_dept_members:
            return True
        flags = auth.derive_role_flags(roles_by_ext.get(ext, {}).get("roletag"))
        return flags["is_manager"] or flags["is_supervisor"] or flags["is_system_owner"]

    return [v for ext, v in seen.items() if not is_hidden_reviewer(ext)]


def is_hidden_reviewer_for(viewer_extension: str, mailbox_extension: str) -> bool:
    """Single-viewer version of the exclusion in viewers_for_mailbox's
    is_hidden_reviewer: admins, Manager/Supervisor/System Owner (implicit,
    org-wide), and anyone with a per-grant or per-department "hide review
    status" flag should stay invisible to the mailbox owner.

    Used by routes/messages.py:set_heard to suppress the native 3CX "heard"
    flip for a hidden reviewer -- unlike the per-viewer review row, "heard"
    is a single mailbox-wide bit, so flipping it on listen would tip off the
    owner that someone reviewed the message even though hide-review-status
    otherwise keeps that invisible.
    """
    if viewer_extension == mailbox_extension:
        return False
    if viewer_extension in app_db.admin_extensions():
        return True
    if viewer_extension in app_db.hidden_review_viewers_for_mailbox(mailbox_extension):
        return True
    for department, mailboxes in app_db.all_department_mailboxes().items():
        if mailbox_extension in mailboxes and viewer_extension in app_db.hidden_review_members(department, mailbox_extension):
            return True
    role = next((r for r in threecx_db.all_roles() if r["extension"] == viewer_extension), None)
    flags = auth.derive_role_flags(role["roletag"] if role else None)
    return flags["is_manager"] or flags["is_supervisor"] or flags["is_system_owner"]


def _native_timestamp_to_iso(raw) -> str | None:
    """3CX's own packed heard_time ("20260806180718.00") to the same naive-UTC
    ISO shape app_db.now_iso() writes for reviewed_at, so a native heard_time
    fallback sorts/compares/formats identically to a real review row."""
    if not raw:
        return None
    digits = str(raw).split(".")[0]
    try:
        dt = datetime.strptime(digits, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def merge_review_status(
    viewers: list[dict],
    reviews: list[dict],
    mailbox_extension: str | None = None,
    native_heard_at=None,
) -> list[dict]:
    """Combine a mailbox's expected reviewers with the review rows recorded
    for one message: [{extension, name, reviewed_at}], reviewed_at is None
    until that viewer has reviewed this particular message. Reviewers who
    have reviewed sort to the top (most recent first), then everyone still
    pending, alphabetically.

    The mailbox owner can listen to their own voicemail straight from their
    desk phone or 3CX's own voicemail system, entirely outside this app --
    that flips 3CX's native `heard` bit but never writes a message_review
    row, since nothing in this app's UI ran to record it. Without a
    fallback, the owner would show "Pending" forever despite having heard
    it. So if the owner has no review row of their own, native_heard_at
    (that message's s_voicemail.heard_time, if heard) stands in for it.
    """
    reviewed_at_by_ext = {r["reviewed_by"]: r["reviewed_at"] for r in reviews}
    native_heard_iso = _native_timestamp_to_iso(native_heard_at)
    merged = [
        {
            **viewer,
            "reviewed_at": reviewed_at_by_ext.get(viewer["extension"])
            or (native_heard_iso if viewer["extension"] == mailbox_extension else None),
        }
        for viewer in viewers
    ]
    reviewed = sorted((v for v in merged if v["reviewed_at"]), key=lambda v: v["reviewed_at"], reverse=True)
    pending = sorted((v for v in merged if not v["reviewed_at"]), key=lambda v: v["name"])
    return reviewed + pending


def _reviewed_at_utc(reviewed_at: str):
    """app_db.now_iso() stores reviewed_at as a naive UTC string (no
    offset); call-log start_time comes back from psycopg2 as an
    offset-aware datetime. Attach UTC explicitly so the two compare
    correctly regardless of the call row's own stored offset."""
    return datetime.strptime(reviewed_at, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


def called_back_after_review(reviewers: list[dict], caller: str) -> dict:
    """For each reviewer who has reviewed the message, checks the call log
    for the earliest outgoing call from that reviewer's extension back to
    the message's caller placed after their reviewed_at -- the "called back"
    signal shown as a blue phone icon beside the review checkmark. Returns
    {extension: call_start_time_iso} only for reviewers who called back;
    reviewers who haven't reviewed or never called back are omitted.
    Deliberately computed per-message (called from the single-message
    /callbacks route) rather than folded into merge_review_status, since
    that's used by the bulk mailbox message list and would turn into an
    N+1 call-log query per reviewer across every message in a mailbox."""
    if not caller:
        return {}
    result = {}
    for r in reviewers:
        if not r["reviewed_at"]:
            continue
        reviewed_at = _reviewed_at_utc(r["reviewed_at"])
        calls = threecx_db.call_history_with_counterpart(r["extension"], caller)
        after = [c for c in calls if c["calltype"] == 3 and c["start_time"] and c["start_time"] > reviewed_at]
        if after:
            earliest = min(after, key=lambda c: c["start_time"])
            result[r["extension"]] = earliest["start_time"].isoformat()
    return result
