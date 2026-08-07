"""
HTML body for the "new voicemail" SMTP notification (notifications.py).
Colors/type are pulled from static/styles.css's tokens so the email reads
as the same product as the web app, not a separate visual identity.

Table-based markup with inline styles throughout -- Outlook's desktop
renderer (Word's HTML engine, not a browser) ignores flexbox/grid and
external/`<style>` block CSS, so anything that has to survive there gets
laid out with tables and styled inline.
"""
import html
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from . import app_db, config, threecx_db


def _brand_name() -> str:
    """Read fresh on every send (not cached at import time) so an admin's
    edit in Settings > General takes effect on the next voicemail without a
    restart -- same as transcription/notification settings."""
    return app_db.get_general_settings()["brand_name"]

# Same packed "YYYYMMDDHHMMSS.ss" (UTC) format as s_voicemail.created_time
# elsewhere -- see routes/yealink.py's _fmt_time docstring for the source
# convention. Mirrored rather than imported since that one returns a
# terser, device-screen format.
_PACKED_TIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")

# Recipients read this in their inbox, not on a desk phone, but they're the
# same staff -- see config.YEALINK_TIME_ZONE's docstring.
_LOCAL_TZ = ZoneInfo(config.YEALINK_TIME_ZONE)


def _parse_packed_time(value) -> datetime | None:
    match = _PACKED_TIME_RE.match(str(value or ""))
    if not match:
        return None
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _fmt_time(value) -> str:
    dt = _parse_packed_time(value)
    if dt is None:
        return str(value or "")
    return dt.astimezone(_LOCAL_TZ).strftime("%B %-d, %Y · %-I:%M %p")


# Thresholds for the digest's staleness dot -- still-unread voicemail older
# than this gets flagged so it doesn't quietly sit for days. Amber first
# (something to get to today), red once it's been over 5 business-ish days.
_STALE_WARN_AFTER = timedelta(hours=24)
_STALE_CRITICAL_AFTER = timedelta(days=5)


def _staleness_dot(created_time) -> str:
    dt = _parse_packed_time(created_time)
    if dt is None:
        return ""
    age = datetime.now(timezone.utc) - dt
    if age >= _STALE_CRITICAL_AFTER:
        color = "#d93025"  # red
    elif age >= _STALE_WARN_AFTER:
        color = "#e6a700"  # amber
    else:
        return ""
    return f'<span style="color:{color};font-size:18px;line-height:1;">&#9679;</span>'


def _transcript_excerpt(transcription: str, max_chars: int = 160) -> str:
    text = (transcription or "").strip()
    if not text:
        return ""
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "…"
    return text


def _mailbox_name(extension: str) -> str:
    row = threecx_db.by_extension(extension)
    if not row:
        return f"Ext. {extension}"
    name = f"{(row['firstname'] or '').strip()} {(row['lastname'] or '').strip()}".strip()
    return name or f"Ext. {extension}"


_TEMPLATE = """\
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:100%;border-collapse:collapse;border:1px solid #d7d3d3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<tr>
<td style="background-color:#004961;padding:24px 32px;">
<p style="margin:0;font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:#e9f8ff;font-weight:bold;">New Voicemail</p>
<h1 style="margin:8px 0 0 0;font-family:Georgia,'Iowan Old Style','Palatino Linotype','Times New Roman',serif;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;">You have a message from<br/><span style="color:#ffffff;">{caller_display}</span></h1>
</td>
</tr>

<tr>
<td style="padding:20px 32px 4px 32px;background-color:#eae9e9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
<tr>
<td style="padding:9px 0;border-bottom:1px solid #d7d3d3;font-size:11px;color:#605d5d;text-transform:uppercase;letter-spacing:0.06em;width:90px;vertical-align:top;">From</td>
<td style="padding:9px 0;border-bottom:1px solid #d7d3d3;">
<p style="margin:0;font-size:14px;color:#201e1d;font-weight:bold;">{caller_display}</p>
<p style="margin:2px 0 0 0;font-size:13px;color:#605d5d;">{caller_sub}</p>
</td>
</tr>
<tr>
<td style="padding:9px 0;border-bottom:1px solid #d7d3d3;font-size:11px;color:#605d5d;text-transform:uppercase;letter-spacing:0.06em;vertical-align:top;">Mailbox</td>
<td style="padding:9px 0;border-bottom:1px solid #d7d3d3;">
<p style="margin:0;font-size:14px;color:#201e1d;font-weight:bold;">{mailbox_name}</p>
<p style="margin:2px 0 0 0;font-size:13px;color:#605d5d;">Ext. {extension}</p>
</td>
</tr>
<tr>
<td style="padding:9px 0;font-size:11px;color:#605d5d;text-transform:uppercase;letter-spacing:0.06em;vertical-align:top;">Time</td>
<td style="padding:9px 0;font-size:14px;color:#201e1d;">{time_display}</td>
</tr>
</table>
</td>
</tr>

<tr>
<td style="padding:20px 32px 4px 32px;background-color:#eae9e9;">
<p style="margin:0 0 8px 0;font-size:11px;color:#605d5d;text-transform:uppercase;letter-spacing:0.06em;">Transcription</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
<tr>
<td style="background-color:#e9f8ff;border-left:3px solid #0088b0;padding:14px 16px;font-size:14px;line-height:1.55;color:#201e1d;">
<p style="margin:0;">{transcription}</p>
</td>
</tr>
</table>
</td>
</tr>

<tr>
<td style="padding:26px 32px 8px 32px;background-color:#eae9e9;text-align:center;" align="center">
<table role="presentation" cellpadding="0" cellspacing="0" border="0" style="margin:0 auto;">
<tr>
<td bgcolor="#0088b0" style="background-color:#0088b0;border-radius:4px;" align="center">
<a href="{listen_url}" style="display:inline-block;padding:13px 28px;font-size:14px;font-weight:bold;color:#ffffff;text-decoration:none;">Listen to Voicemail</a>
</td>
</tr>
</table>
<p style="margin:12px 0 0 0;font-size:11px;color:#605d5d;word-break:break-all;">Or paste this link into your browser:<br/><a href="{listen_url}" style="color:#605d5d;">{listen_url}</a></p>
</td>
</tr>

<tr>
<td style="padding:24px 32px 28px 32px;background-color:#eae9e9;border-top:1px solid #d7d3d3;text-align:center;" align="center">
<p style="margin:0;font-size:11px;line-height:1.6;color:#605d5d;">This notification was sent from {brand_name} Voicemail Manager.</p>
</td>
</tr>
</table>
"""


_DIGEST_TEMPLATE = """\
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:100%;border-collapse:collapse;border:1px solid #d7d3d3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<tr>
<td style="background-color:#004961;padding:24px 32px;">
<h1 style="margin:8px 0 0 0;font-family:Georgia,'Iowan Old Style','Palatino Linotype','Times New Roman',serif;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;">You have {count} unread voicemail{plural}</h1>
</td>
</tr>
{sections}
<tr>
<td style="padding:24px 32px 28px 32px;background-color:#eae9e9;border-top:1px solid #d7d3d3;text-align:center;" align="center">
<p style="margin:0;font-size:11px;line-height:1.6;color:#605d5d;">This notification was sent from {brand_name} Voicemail Manager.</p>
</td>
</tr>
</table>
"""

_DIGEST_SECTION_TEMPLATE = """\
<tr>
<td style="padding:20px 32px 4px 32px;background-color:#eae9e9;">
<p style="margin:0 0 8px 0;font-size:11px;color:#605d5d;text-transform:uppercase;letter-spacing:0.06em;">{mailbox_name} &middot; {count} unread</p>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
{rows}
</table>
</td>
</tr>
"""

_DIGEST_ROW_TEMPLATE = """\
<tr>
<td style="padding:14px 0;{border_style}">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
<tr>
<td style="width:20px;vertical-align:top;padding-top:2px;">{dot}</td>
<td style="vertical-align:top;">
<p style="margin:0;font-size:14px;color:#201e1d;font-weight:bold;">{caller_display}</p>
<p style="margin:2px 0 0 0;font-size:13px;color:#605d5d;">{caller_sub} &middot; {time_display}</p>
{transcript_row}
</td>
<td style="vertical-align:top;text-align:right;white-space:nowrap;" align="right">
<a href="{listen_url}" style="display:inline-block;padding:9px 18px;font-size:13px;font-weight:bold;color:#ffffff;background-color:#0088b0;border-radius:4px;text-decoration:none;">Acknowledge</a>
</td>
</tr>
</table>
</td>
</tr>
"""

_DIGEST_TRANSCRIPT_TEMPLATE = """<p style="margin:6px 0 0 0;font-size:12px;color:#605d5d;font-style:italic;">&ldquo;{excerpt}&rdquo;</p>"""


def _digest_rows(mailbox_extension: str, messages: list[dict]) -> str:
    # border-bottom separates one row from the next -- the last row in a
    # section skips it, otherwise it reads as a stray divider line under
    # the final entry with nothing below it but the section's own padding.
    rows = []
    for i, m in enumerate(messages):
        excerpt = _transcript_excerpt(m.get("transcription"))
        rows.append(
            _DIGEST_ROW_TEMPLATE.format(
                dot=_staleness_dot(m["created_time"]),
                caller_display=html.escape(
                    threecx_db.crm_display_name(m["crm_contact"])
                    or m["caller_name"]
                    or m["caller"]
                    or "Unknown caller"
                ),
                caller_sub=html.escape(format_phone(m["caller"] or m["caller_name"] or "")),
                time_display=html.escape(_fmt_time(m["created_time"])),
                transcript_row=_DIGEST_TRANSCRIPT_TEMPLATE.format(excerpt=html.escape(excerpt)) if excerpt else "",
                listen_url=f"{config.PUBLIC_BASE_URL}/app/{mailbox_extension}/{m['id']}",
                border_style="" if i == len(messages) - 1 else "border-bottom:1px solid #d7d3d3;",
            )
        )
    return "".join(rows)


def build_digest(recipient_extension: str, mailbox_messages: dict[str, list[dict]]) -> tuple[str, str]:
    """Returns (subject, html_body) for one recipient's daily digest,
    combining every mailbox they're a notification recipient for that
    currently has unread voicemail into a single email -- one section per
    mailbox -- rather than a separate email per mailbox. mailbox_messages
    maps mailbox_extension -> threecx_db.unread_messages_for_mailbox's
    result (oldest first); both the dict and every message list are already
    known to be non-empty by the caller."""
    recipient_name = _mailbox_name(recipient_extension)
    total = sum(len(messages) for messages in mailbox_messages.values())
    sections = "".join(
        _DIGEST_SECTION_TEMPLATE.format(
            mailbox_name=html.escape(_mailbox_name(mailbox_extension)),
            count=len(messages),
            rows=_digest_rows(mailbox_extension, messages),
        )
        for mailbox_extension, messages in mailbox_messages.items()
    )
    body = _DIGEST_TEMPLATE.format(
        count=total,
        plural="" if total == 1 else "s",
        recipient_name=html.escape(recipient_name),
        sections=sections,
        brand_name=html.escape(_brand_name()),
    )
    subject = f"[ {total} ] unread voicemail{'' if total == 1 else 's'} - Voicemail Manager"
    return subject, body


def format_phone(raw: str) -> str:
    """Formats a 10-digit NANP number, or an 11-digit one with a leading '1'
    country code, as "1 (xxx) xxx-xxxx". Anything else (extensions,
    international numbers, already-formatted text) is returned unchanged --
    there's no reliable way to reformat what we don't recognize."""
    digits = re.sub(r"\D", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    elif len(digits) != 10:
        return raw
    return f"1 ({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"


def build(message: dict) -> tuple[str, str]:
    """Returns (subject, html_body) for a just-arrived s_voicemail row (the
    same dict voicemail_watcher hands every handler). Re-fetches the row
    via threecx_db.get_message rather than trusting the passed-in dict,
    since transcription is written by transcription_worker's handler --
    which runs before this one in main.py's registration order -- and isn't
    present on the original watcher payload."""
    full = threecx_db.get_message(message["id"])
    extension = full["callee"]

    did = full["caller"] or full["caller_name"] or "Unknown number"
    caller_display = (
        threecx_db.crm_display_name(full["crm_contact"])
        or full["caller_name"]
        or full["caller"]
        or "Unknown caller"
    )
    # Always the raw DID, even when caller_display resolved from caller_name
    # above -- otherwise a call with no CRM match shows the same text on both
    # lines instead of giving the actual callback number.
    caller_sub = format_phone(did)
    mailbox_name = _mailbox_name(extension)
    time_display = _fmt_time(full["created_time"])
    transcription = full["transcription"] or "Transcription not yet available."
    listen_url = f"{config.PUBLIC_BASE_URL}/app/{extension}/{full['id']}"

    # caller_name/crm_contact/transcription ultimately come from SIP headers
    # and transcribed audio -- untrusted text -- so it's escaped before
    # going into the HTML, unlike listen_url which we build ourselves.
    body = _TEMPLATE.format(
        caller_display=html.escape(caller_display),
        caller_sub=html.escape(caller_sub),
        mailbox_name=html.escape(mailbox_name),
        extension=html.escape(extension),
        time_display=html.escape(time_display),
        transcription=html.escape(transcription),
        listen_url=listen_url,
        brand_name=html.escape(_brand_name()),
    )
    subject = f"New Voicemail - {caller_display} - {format_phone(did)}"
    return subject, body
