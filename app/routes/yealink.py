"""
Yealink XML Browser routes -- visual voicemail on the phone's own screen
(see notes/README.md, notes/xmldevguide.pdf). The phone only ever does HTTP
GET (no POST body, no cookies), so every bit of state -- auth token, which
mailbox, which message-id snapshot -- rides in the query string. See
app/auth.py's create_yealink_token/verify_yealink_token for the token, and
the "Unread/Read/All" section of the design plan for why message lists are
snapshotted by id rather than re-queried on every navigation.

Every route below returns HTTP 200 with an XML screen even for auth/access
failures (an error TextScreen) -- the phone has no way to usefully render a
bare 4xx. The one exception is /vvm/audio/{id}.wav, which the phone fetches
directly via Wav.Play rather than rendering, so ordinary HTTP errors there
are fine (matches routes/messages.py's stream_audio convention).

Softkey layout: the same physical slot (index 4) is the "Back" button
wherever there's somewhere to go back to. There is deliberately no
onscreen "Exit" softkey anywhere -- the PDF documents "SoftKey:Exit" as
"Exit from the current XML screen" only, i.e. it pops one entry off the
phone's own internal navigation history, not a jump to idle. Since every
Back link here is a fresh HTTP fetch rather than a true history
back-navigation, that history only ever grows forward, so a SoftKey:Exit
button placed anywhere just pops into a screen already shown -- it can
never reliably reach idle. The phone's physical Cancel/X key already does
reach idle by default on every screen here (confirmed in the spec: with no
`cancelAction` attribute set, Cancel returns straight to idle) -- so that
hardware key, not an onscreen button, is how someone actually leaves the
app at the top of the tree.
"""
import io
import logging
import re
import secrets
import wave
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Response

from .. import access, app_db, auth, config, threecx_db, threecx_notify, yealink_xml
from .mailboxes import _is_unheard

logger = logging.getLogger(__name__)

router = APIRouter()

MEDIA_TYPE = "text/xml"

# Caps how many message ids a filter snapshot embeds in the URL -- a
# pragmatic safety valve (Yealink's own docs don't state a hard GET-length
# limit), not a hard product requirement.
MAX_SNAPSHOT = 40

# Yealink's TextMenu spec caps items at 30 total (confirmed in the PDF: "up
# to 30" menu items) -- matters for the mailbox list on org-wide
# (manager/system-owner) accounts, which can otherwise see 50+ mailboxes.
MAX_MENU_ITEMS = 30

# Fixed physical softkey slot for "Back" -- see module docstring. Screens at
# the top of the tree (nothing to go back to) simply omit this slot; the
# phone's own hardware Cancel/X key is what leaves to idle from there.
BACK_INDEX = 4

# Matches the range already in use across voicemail.pinnumber today (shortest
# live PIN is 4 digits, longest is 6) -- not a 3CX-documented limit, just a
# sane bound so a mistyped/pasted value can't wedge in an absurd PIN via this
# screen. The column itself is varchar(255), no DB-level constraint to lean on.
MIN_PIN_LEN = 4
MAX_PIN_LEN = 15


def _back_softkey(url: str) -> tuple[int, str, str]:
    return (BACK_INDEX, "Back", url)

def _url(path: str, **params) -> str:
    # `,` is left unescaped (safe=",") -- it's not a query-string delimiter,
    # and every MenuItem in a message list repeats the full `ids` snapshot
    # in its own link, so percent-encoding every comma to %2C (3x the
    # bytes) was blowing the response past Yealink's documented 10KB
    # per-XML-object cap for any list long enough to matter (i.e. exactly
    # the "Read"/"All" categories, which accumulate over time, while
    # "Unread" stays short) -- the object then silently fails to render.
    query = "&".join(
        f"{k}={quote(str(v), safe=',')}" for k, v in params.items() if v is not None
    )
    return f"{config.PUBLIC_BASE_URL}{path}?{query}" if query else f"{config.PUBLIC_BASE_URL}{path}"


def _ids_param(ids: list[int]) -> str:
    return ",".join(str(i) for i in ids)


def _parse_ids(ids: str) -> list[int]:
    return [int(part) for part in ids.split(",") if part.strip().isdigit()]


# 3CX's created_time is a packed "YYYYMMDDHHMMSS.ss" string (UTC), not a
# real timestamp column -- same convention app.js's own parseServerTimestamp
# decodes for the web UI. Mirrored here rather than reused since that's
# client-side JS.
_PACKED_TIME_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})(\d{2})")

# Desk phones are all in one timezone -- see config.YEALINK_TIME_ZONE.
_PHONE_TZ = ZoneInfo(config.YEALINK_TIME_ZONE)


def _fmt_time(value) -> str:
    if value is None:
        return ""
    match = _PACKED_TIME_RE.match(str(value))
    if not match:
        return str(value)
    year, month, day, hour, minute, second = (int(g) for g in match.groups())
    dt = datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)
    return dt.astimezone(_PHONE_TZ).strftime("%Y-%m-%d %-I:%M %p")


def _fmt_reviewed_at(value) -> str:
    # app_db.now_iso() stores a naive UTC "YYYY-MM-DDTHH:MM:SS" string.
    if not value:
        return ""
    dt = datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc)
    return dt.astimezone(_PHONE_TZ).strftime("%Y-%m-%d %-I:%M %p")


def _session(extension: str) -> dict:
    return {"extension": extension, **access.current_identity(extension)}


def _xml(body: str) -> Response:
    return Response(content=body, media_type=MEDIA_TYPE)


def _pin_prompt_response(ext: str, error: str | None = None) -> Response:
    return _xml(yealink_xml.pin_prompt(ext, _url("/vvm/auth/check"), error=error))


def _access_denied_response() -> Response:
    # Dead-end screen reachable from several different contexts with no
    # single obvious "back to" URL -- no softkey at all; hardware Cancel
    # leaves it same as everywhere else.
    return _xml(
        yealink_xml.text_screen("Access Denied", "You do not have access to this mailbox.", [])
    )


def _delete_not_allowed_response() -> Response:
    return _xml(
        yealink_xml.text_screen(
            "Access Denied",
            "Only a Supervisor or Manager can delete voicemails from this mailbox.",
            [],
        )
    )


def _not_found_response() -> Response:
    return _xml(yealink_xml.text_screen("Not Found", "Message not found.", []))


# --- Screen 1: PIN auth -------------------------------------------------------

@router.get("/vvm/menu")
def menu(ext: str, token: str | None = None):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    return _entry_screen(extension, token)


@router.get("/vvm/auth/check")
def auth_check(ext: str, pin: str):
    if app_db.is_locked_out(ext):
        return _pin_prompt_response(
            ext, error=f"Too many attempts. Wait {config.LOGIN_LOCKOUT_MINUTES} min."
        )
    user = threecx_db.authenticate(ext, pin)
    app_db.record_login_attempt(ext, ok=user is not None)
    if user is None:
        return _pin_prompt_response(ext, error="Incorrect PIN, try again")
    token = auth.create_yealink_token(user["extension"])
    return _entry_screen(user["extension"], token)


def _entry_screen(extension: str, token: str) -> Response:
    boxes = access.accessible_mailboxes(_session(extension))
    if len(boxes) <= 1:
        # Only the personal mailbox exists -- skip the mailbox list per the
        # spec ("if no mailboxes are delegated, skip and go to third screen
        # if possible") and land straight on the Unread/Read/All menu. There
        # is no earlier screen in this case, so no Back softkey there.
        return _filter_menu_response(extension, extension, token, show_back=False)
    return _mailbox_list_response(extension, boxes, token)


# --- Screen 2: mailbox list (personal first, then delegated/group A-Z) ------

@router.get("/vvm/mailboxes")
def mailboxes(ext: str, token: str):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    boxes = access.accessible_mailboxes(_session(extension))
    return _mailbox_list_response(extension, boxes, token)


def _unread_count(box_extension: str) -> int:
    # Same per-mailbox N+1 pattern routes/mailboxes.py:list_mailboxes already
    # uses for its own sidebar unread count -- no bulk query exists for this,
    # and this list is capped at MAX_MENU_ITEMS mailboxes anyway.
    messages = threecx_db.messages_for_mailbox(box_extension)
    return sum(1 for m in messages if _is_unheard(m["heard"]))


def _mailbox_list_response(extension: str, boxes: list[dict], token: str) -> Response:
    personal = next(b for b in boxes if b["source"] == "personal")
    others = sorted(
        (b for b in boxes if b["extension"] != personal["extension"]),
        key=lambda b: (b["name"] or b["extension"]),
    )[: MAX_MENU_ITEMS - 1]
    items = [
        (
            f"Personal Voicemail ({_unread_count(personal['extension'])})",
            _url(f"/vvm/mailbox/{personal['extension']}/filter", ext=extension, token=token),
        )
    ]
    for box in others:
        label = box["name"] or box["extension"]
        prompt = f"{label} ({_unread_count(box['extension'])})"
        items.append((prompt, _url(f"/vvm/mailbox/{box['extension']}/filter", ext=extension, token=token)))
    # No Back slot -- this is the top of the tree once authenticated;
    # hardware Cancel leaves to idle from here (see module docstring).
    softkeys = [(1, "Open", "SoftKey:Select")]
    return _xml(yealink_xml.text_menu("Voicemail Mailboxes", items, softkeys))


# --- Screen 2.5: Unread / Read / All ------------------------------------------

@router.get("/vvm/mailbox/{box_ext}/filter")
def mailbox_filter(box_ext: str, ext: str, token: str):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    session = _session(extension)
    if not access.can_access_mailbox(session, box_ext):
        return _access_denied_response()
    show_back = len(access.accessible_mailboxes(session)) > 1
    return _filter_menu_response(extension, box_ext, token, show_back=show_back)


def _filter_menu_response(extension: str, box_ext: str, token: str, show_back: bool) -> Response:
    messages = threecx_db.messages_for_mailbox(box_ext)
    unread_ids = [m["id"] for m in messages if _is_unheard(m["heard"])]
    read_ids = [m["id"] for m in messages if not _is_unheard(m["heard"])]
    all_ids = [m["id"] for m in messages]

    def messages_url(ids: list[int]) -> str:
        return _url(
            f"/vvm/mailbox/{box_ext}/messages",
            ext=extension,
            token=token,
            ids=_ids_param(ids[:MAX_SNAPSHOT]),
        )

    items = [
        (f"Unread ({len(unread_ids)})", messages_url(unread_ids)),
        (f"Read ({len(read_ids)})", messages_url(read_ids)),
        (f"All ({len(all_ids)})", messages_url(all_ids)),
    ]
    is_personal = box_ext == extension
    if is_personal:
        # Own mailbox only -- changing a delegated/group mailbox's PIN isn't
        # this person's PIN to change, so the item is omitted entirely there
        # rather than just being access-denied if guessed.
        items.append(("Change PIN", _url(f"/vvm/mailbox/{box_ext}/pin/change", ext=extension, token=token)))
    title = "Personal Voicemail" if is_personal else box_ext
    softkeys = [(1, "Select", "SoftKey:Select")]
    if show_back:
        softkeys.append(_back_softkey(_url("/vvm/mailboxes", ext=extension, token=token)))
    # else: this is the top of the tree (mailbox list was skipped) -- no
    # Back slot, hardware Cancel leaves to idle same as the mailbox list.
    return _xml(yealink_xml.text_menu(title, items, softkeys))


# --- Change PIN (personal mailbox only -- never delegated/group mailboxes) --

@router.get("/vvm/mailbox/{box_ext}/pin/change")
def pin_change(box_ext: str, ext: str, token: str):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    if box_ext != extension:
        # Not access-deniable in the ordinary sense -- a delegated viewer
        # can legitimately *see* this mailbox's messages, just never its
        # PIN. Same dead-end screen as an actual access failure either way.
        return _access_denied_response()
    submit_url = _url(f"/vvm/mailbox/{box_ext}/pin/set", ext=extension, token=token)
    return _xml(yealink_xml.pin_change_prompt(submit_url))


@router.get("/vvm/mailbox/{box_ext}/pin/set")
def pin_set(box_ext: str, ext: str, token: str, pin1: str = "", pin2: str = ""):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    if box_ext != extension:
        return _access_denied_response()

    submit_url = _url(f"/vvm/mailbox/{box_ext}/pin/set", ext=extension, token=token)

    def retry(error: str) -> Response:
        return _xml(yealink_xml.pin_change_prompt(submit_url, error=error))

    if pin1 != pin2:
        return retry("PINs did not match, try again")
    if not pin1.isdigit():
        return retry("PIN must be numeric, try again")
    if not (MIN_PIN_LEN <= len(pin1) <= MAX_PIN_LEN):
        return retry(f"PIN must be {MIN_PIN_LEN}-{MAX_PIN_LEN} digits, try again")

    ok = threecx_db.set_pin(extension, pin1)
    if not ok:
        logger.error("Yealink PIN change: no voicemail row updated for extension %s", extension)
        return retry("Could not update PIN, try again")

    back_uri = _url(f"/vvm/mailbox/{box_ext}/filter", ext=extension, token=token)
    return _xml(
        yealink_xml.text_screen(
            "PIN Changed", "Your voicemail PIN has been updated.", [_back_softkey(back_uri)]
        )
    )


# --- Screen 3: message list (renders a frozen id snapshot) -------------------

@router.get("/vvm/mailbox/{box_ext}/messages")
def mailbox_messages(box_ext: str, ext: str, token: str, ids: str = ""):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    if not access.can_access_mailbox(_session(extension), box_ext):
        return _access_denied_response()

    back_uri = _url(f"/vvm/mailbox/{box_ext}/filter", ext=extension, token=token)
    items = []
    for mid in _parse_ids(ids)[:MAX_MENU_ITEMS]:
        m = threecx_db.get_message(mid)
        if m is None or m["callee"] != box_ext:
            continue  # snapshot ids are untrusted, never authorizing -- just skip
        unread = _is_unheard(m["heard"])
        label = m["caller_name"] or m["caller"] or "Unknown"
        prompt = f"{'* ' if unread else ''}{label} - {_fmt_time(m['created_time'])}"
        uri = _url(f"/vvm/msg/{mid}", ext=extension, token=token, ids=ids)
        items.append((prompt, uri))

    if not items:
        return _xml(
            yealink_xml.text_screen("Voicemail", "No messages.", [_back_softkey(back_uri)])
        )

    softkeys = [(1, "Open", "SoftKey:Select"), _back_softkey(back_uri)]
    return _xml(yealink_xml.text_menu("Voicemail", items, softkeys))


# --- Screen 4: message detail (transcript + play/stop + call-back) ----------

@router.get("/vvm/msg/{message_id}")
def message_detail(message_id: int, ext: str, token: str, ids: str = ""):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    message = threecx_db.get_message(message_id)
    if message is None:
        return _not_found_response()
    session = _session(extension)
    if not access.can_access_mailbox(session, message["callee"]):
        return _access_denied_response()
    return _detail_response(extension, token, message, ids, access.can_delete_from_mailbox(session, message["callee"]))


def _detail_response(extension: str, token: str, message: dict, ids: str, can_delete: bool = True) -> Response:
    mid = message["id"]
    caller_label = message["caller_name"] or message["caller"] or "Unknown"
    duration_s = int(message["duration"] or 0) // 1000
    transcript = message["transcription"] or "No transcription available."

    reviews = app_db.get_reviews(mid)  # ordered by reviewed_at ascending -- [0] is first-ever reviewer
    reviewed_line = ""
    if reviews:
        first = reviews[0]
        directory_by_ext = {row["extension"]: row for row in threecx_db.directory()}
        row = directory_by_ext.get(first["reviewed_by"])
        reviewer_name = " ".join(p for p in (row["firstname"], row["lastname"]) if p).strip() if row else first["reviewed_by"]
        reviewed_line = f"Reviewed by: {reviewer_name} ({_fmt_reviewed_at(first['reviewed_at'])})\n"

    text = (
        f"From: {caller_label}\n"
        f"Received: {_fmt_time(message['created_time'])}\n"
        f"Duration: {duration_s}s\n"
        f"{reviewed_line}\n"
        f"{transcript}"
    )

    # `_` is a cache-busting nonce, unused server-side -- identical audio
    # URLs across repeated Play presses in one session let some phones
    # replay a locally cached copy alongside the fresh stream instead of
    # just the fresh one, which sounded like the message playing twice
    # (later ruled out as the actual cause, but harmless to keep).
    audio_url = _url(
        f"/vvm/audio/{mid}.wav", ext=extension, token=token, **{"_": secrets.token_hex(4)}
    )
    # Play softkey fires Wav.Play directly rather than routing through our
    # own /play endpoint first -- cuts the GET-then-Execute-then-GET
    # round trip down to just the one audio fetch, in case the extra hop
    # itself was implicated in the double-playback bug (see notes/README.md).
    play_uri = f"Wav.Play:{audio_url}"
    delete_confirm_uri = _url(f"/vvm/msg/{mid}/delete/confirm", ext=extension, token=token, ids=ids)
    back_uri = _url(f"/vvm/mailbox/{message['callee']}/messages", ext=extension, token=token, ids=ids)
    softkeys = [(1, "Play", play_uri)]
    if message["caller"]:
        softkeys.append((2, "Call", f"Dial:{message['caller']}"))
    if can_delete:
        softkeys.append((3, "Delete", delete_confirm_uri))
    softkeys.append(_back_softkey(back_uri))

    # doneAction binds the phone's OK/check key -- per the Yealink spec this
    # is unconditional whenever the key exists on the model, regardless of
    # LockIn. Leaving it unset makes the phone try to navigate to an empty
    # URI and show "Invalid URI" if OK is pressed. Bound to the same
    # Wav.Play URI as the Play softkey so OK plays the message too, same
    # as Play -- still the direct Wav.Play URI, no intermediate /play hop.
    return _xml(
        yealink_xml.text_screen(caller_label, text, softkeys, done_action=play_uri, lock_in=True)
    )


# --- Delete (confirm, then the real native RPC via threecx_notify.notify_delete) --

@router.get("/vvm/msg/{message_id}/delete/confirm")
def delete_confirm(message_id: int, ext: str, token: str, ids: str = ""):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    message = threecx_db.get_message(message_id)
    if message is None:
        return _not_found_response()
    session = _session(extension)
    if not access.can_access_mailbox(session, message["callee"]):
        return _access_denied_response()
    if not access.can_delete_from_mailbox(session, message["callee"]):
        return _delete_not_allowed_response()

    caller_label = message["caller_name"] or message["caller"] or "Unknown"
    detail_uri = _url(f"/vvm/msg/{message_id}", ext=extension, token=token, ids=ids)
    delete_uri = _url(f"/vvm/msg/{message_id}/delete", ext=extension, token=token, ids=ids)
    text = f"Delete this voicemail from {caller_label}?\n\nThis cannot be undone."
    softkeys = [(1, "Confirm", delete_uri), (BACK_INDEX, "Cancel", detail_uri)]
    return _xml(yealink_xml.text_screen("Confirm Delete", text, softkeys, lock_in=True))


@router.get("/vvm/msg/{message_id}/delete")
def delete_message(message_id: int, ext: str, token: str, ids: str = ""):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        return _pin_prompt_response(ext)
    message = threecx_db.get_message(message_id)
    if message is None:
        return _not_found_response()
    session = _session(extension)
    if not access.can_access_mailbox(session, message["callee"]):
        return _access_denied_response()
    if not access.can_delete_from_mailbox(session, message["callee"]):
        return _delete_not_allowed_response()

    box_ext = message["callee"]
    try:
        threecx_notify.notify_delete(message_id, box_ext)
    except threecx_notify.NotifyError:
        logger.exception("Yealink delete: failed for message %s", message_id)
        retry_uri = _url(f"/vvm/msg/{message_id}", ext=extension, token=token, ids=ids)
        return _xml(
            yealink_xml.text_screen(
                "Delete Failed",
                "Could not delete this voicemail. Please try again.",
                [(BACK_INDEX, "Back", retry_uri)],
            )
        )

    # Unlike the Unread/Read/All snapshot (which must NOT shift under a
    # listener -- see the module docstring's "stability requirement"),
    # deletion is deliberate and irreversible, so the list should reflect it
    # right away rather than reusing the now-stale `ids` snapshot. Send back
    # to a fresh filter-menu query instead.
    show_back = len(access.accessible_mailboxes(_session(extension))) > 1
    return _filter_menu_response(extension, box_ext, token, show_back=show_back)


# --- Audio file (what Wav.Play actually fetches -- Play softkey points here
# directly now, see _detail_response) ------------------------------------------

@router.get("/vvm/audio/{message_id}.wav")
def audio(message_id: int, ext: str, token: str):
    extension = auth.verify_yealink_token(token)
    if extension is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    message = threecx_db.get_message(message_id)
    if message is None:
        raise HTTPException(status_code=404, detail="Message not found")
    if not access.can_access_mailbox(_session(extension), message["callee"]):
        raise HTTPException(status_code=403, detail="No access to this message")
    try:
        path = config.resolve_voicemail_path(message["callee"], message["wav_file"])
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid audio path")
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio file not found")

    # Mark-heard lives here rather than in a separate /play step -- the Play
    # softkey fires Wav.Play straight at this URL, so this is the only point
    # left where "the phone actually played it" is observable. Same RPC path
    # routes/messages.py's own heard-toggle button uses, so the real SIP
    # NOTIFY (MWI) fires the same way it would from the web app.
    # (Confirmed NOT the cause of the double-playback bug -- disabling this
    # entirely was tested and the doubling persisted. See notes/README.md.)
    # Best-effort: must not block serving the audio if 3CX's RPC is slow.
    try:
        threecx_notify.notify_heard(message_id, message["callee"], True)
    except threecx_notify.NotifyError:
        logger.exception("Yealink audio: mark-heard failed for message %s", message_id)

    return _xml_response_wav(path)


def _xml_response_wav(path) -> Response:
    # 3CX writes an 18-byte `fmt ` chunk (16-byte PCM fmt plus a padded
    # cbSize field) rather than the canonical 16-byte PCM fmt chunk.
    # Several Yealink desk phones (confirmed on both a T4x and a T5xW, so
    # not model-specific) have a WAV parser for local Wav.Play that
    # hardcodes a 16-byte skip after the fmt header and assumes `data`
    # starts immediately after -- with an 18-byte fmt chunk that lands 2
    # bytes into the data chunk's own sub-header, and the resulting
    # misparse plays the message twice. Re-muxing through `wave` (which
    # always writes a canonical 16-byte fmt chunk) fixes the framing
    # without touching the actual audio samples.
    reader = wave.open(str(path), "rb")
    try:
        params = reader.getparams()
        frames = reader.readframes(reader.getnframes())
    finally:
        reader.close()
    buf = io.BytesIO()
    writer = wave.open(buf, "wb")
    writer.setparams(params)
    writer.writeframes(frames)
    writer.close()
    return Response(content=buf.getvalue(), media_type="audio/wav")
