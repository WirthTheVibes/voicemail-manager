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
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from . import config, threecx_db

# TODO(branding): hardcoded placeholder. Replace with a DB-backed setting
# (same pattern as app_db.get_transcription_settings) once that's wired up,
# so each deployment can set its own company name from the admin UI instead
# of editing source.
BRAND_NAME = "Your Phone System"

# Same packed "YYYYMMDDHHMMSS.ss" (UTC) format as s_voicemail.created_time
# elsewhere -- see routes/yealink.py's _fmt_time docstring for the source
# convention. Mirrored rather than imported since that one returns a
# terser, device-screen format.
_PACKED_TIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")

# Recipients read this in their inbox, not on a desk phone, but they're the
# same staff -- see config.YEALINK_TIME_ZONE's docstring.
_LOCAL_TZ = ZoneInfo(config.YEALINK_TIME_ZONE)


def _fmt_time(value) -> str:
    match = _PACKED_TIME_RE.match(str(value or ""))
    if not match:
        return str(value or "")
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.astimezone(_LOCAL_TZ).strftime("%B %-d, %Y · %-I:%M %p")


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
<p style="margin:0;font-size:11px;line-height:1.6;color:#605d5d;">This message was recorded by the {brand_name} phone system (3CX).</p>
</td>
</tr>
</table>
"""


_DIGEST_TEMPLATE = """\
<table role="presentation" width="600" cellpadding="0" cellspacing="0" border="0" style="width:600px;max-width:100%;border-collapse:collapse;border:1px solid #d7d3d3;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;">
<tr>
<td style="background-color:#004961;padding:24px 32px;">
<p style="margin:0;font-size:11px;letter-spacing:0.06em;text-transform:uppercase;color:#e9f8ff;font-weight:bold;">Daily Voicemail Digest</p>
<h1 style="margin:8px 0 0 0;font-family:Georgia,'Iowan Old Style','Palatino Linotype','Times New Roman',serif;font-size:22px;line-height:1.3;color:#ffffff;font-weight:600;">{count} unread voicemail{plural} for<br/><span style="color:#ffffff;">{mailbox_name}</span></h1>
</td>
</tr>
<tr>
<td style="padding:20px 32px 24px 32px;background-color:#eae9e9;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
{rows}
</table>
</td>
</tr>
<tr>
<td style="padding:24px 32px 28px 32px;background-color:#eae9e9;border-top:1px solid #d7d3d3;text-align:center;" align="center">
<p style="margin:0;font-size:11px;line-height:1.6;color:#605d5d;">This message was recorded by the {brand_name} phone system (3CX).</p>
</td>
</tr>
</table>
"""

_DIGEST_ROW_TEMPLATE = """\
<tr>
<td style="padding:14px 0;border-bottom:1px solid #d7d3d3;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="border-collapse:collapse;">
<tr>
<td style="vertical-align:top;">
<p style="margin:0;font-size:14px;color:#201e1d;font-weight:bold;">{caller_display}</p>
<p style="margin:2px 0 0 0;font-size:13px;color:#605d5d;">{caller_sub} &middot; {time_display} &middot; {duration}s</p>
</td>
<td style="vertical-align:top;text-align:right;white-space:nowrap;" align="right">
<a href="{listen_url}" style="display:inline-block;padding:9px 18px;font-size:13px;font-weight:bold;color:#ffffff;background-color:#0088b0;border-radius:4px;text-decoration:none;">Listen</a>
</td>
</tr>
</table>
</td>
</tr>
"""


def build_digest(extension: str, messages: list[dict]) -> tuple[str, str]:
    """Returns (subject, html_body) for the daily digest of a mailbox's
    still-unread voicemails. messages is threecx_db.unread_messages_for_mailbox's
    result (oldest first), already known to be non-empty by the caller."""
    mailbox_name = _mailbox_name(extension)
    rows = "".join(
        _DIGEST_ROW_TEMPLATE.format(
            caller_display=html.escape(
                threecx_db.crm_display_name(m["crm_contact"])
                or m["caller_name"]
                or m["caller"]
                or "Unknown caller"
            ),
            caller_sub=html.escape(format_phone(m["caller"] or m["caller_name"] or "")),
            time_display=html.escape(_fmt_time(m["created_time"])),
            duration=html.escape(str(m.get("duration") or "0")),
            listen_url=f"{config.PUBLIC_BASE_URL}/app/{extension}/{m['id']}",
        )
        for m in messages
    )
    body = _DIGEST_TEMPLATE.format(
        count=len(messages),
        plural="" if len(messages) == 1 else "s",
        mailbox_name=html.escape(mailbox_name),
        rows=rows,
        brand_name=html.escape(BRAND_NAME),
    )
    subject = f"{len(messages)} unread voicemail{'' if len(messages) == 1 else 's'} — {mailbox_name}"
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
        brand_name=html.escape(BRAND_NAME),
    )
    subject = f"New Voicemail - {caller_display} - {format_phone(did)}"
    return subject, body
