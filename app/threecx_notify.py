"""
Native 3CX MWI notification worker.

A direct SQL write to s_voicemail.heard (threecx_db.set_heard) does not
trigger 3CX's SIP NOTIFY to the phone — the NOTIFY is fired by 3CX's own
application code as a side effect of a specific RPC call, not by DB state.
This module reproduces that RPC call: log in once as the System Owner admin
extension, impersonate the mailbox-owning extension per call, then issue the
legacy MyPhone method-114 RPC 3CX's own web client uses.

Reverse-engineered against 3CX v20.0.9.995 — see notes/3cx-native-notify.md
for the full protocol writeup this implementation follows.
"""
import threading
from urllib.parse import quote

import pyotp
import requests

from . import config, threecx_db

_LOGIN_PATH = "/webclient/api/Login/GetAccessToken"
_TOKEN_PATH = "/connect/token"
_SESSION_PATH = "/webclient/api/MyPhone/session"
_RPC_PATH = "/MyPhone/MPWebService.asmx"
_METHOD_MARK_HEARD = 114
_METHOD_DELETE = 126
_METHOD_RECORD_FILE = 144

# 3CX's REST xapi -- not the legacy MPWebService RPC above, but the same
# OAuth access_token from _AdminSession.impersonate() authenticates it (same
# Authorization: Bearer header the MyPhone session bootstrap uses). Backs
# the greeting file manager (list/activate/delete) -- reverse-engineered
# from the real webclient's Settings > Greetings screen, see
# "greeting delete.har": DELETE .../DeleteGreeting/{file}, then
# PATCH MyUser {"Greetings":[{"Type":"Default","Filename":""}]} to clear the
# now-dangling active pointer -- 3CX does NOT do that second step itself.
_XAPI_GREETINGS_PATH = "/xapi/v1/MyUser/Greetings"
_XAPI_DELETE_GREETING_PATH = "/xapi/v1/MyUser/DeleteGreeting"
_XAPI_MYUSER_PATH = "/xapi/v1/MyUser"

_TIMEOUT = 10


class NotifyError(RuntimeError):
    """The native-notify flow failed, or its effect couldn't be verified."""


# --- Protobuf wire format (see notes/3cx-native-notify.md) -------------------

def _varint(n: int) -> bytes:
    if n < 0:
        raise ValueError("varint encoding requires a non-negative integer")
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _tag(field_number: int, wire_type: int) -> bytes:
    return _varint((field_number << 3) | wire_type)


def _mark_heard_payload(voicemail_id: int, heard: bool) -> bytes:
    inner = (
        _tag(1, 0) + _varint(voicemail_id)
        + _tag(2, 0) + _varint(1 if heard else 0)
    )
    return (
        _tag(1, 0) + _varint(_METHOD_MARK_HEARD)
        + _tag(_METHOD_MARK_HEARD, 2) + _varint(len(inner)) + inner
    )


def _delete_payload(voicemail_id: int) -> bytes:
    inner = _tag(1, 0) + _varint(voicemail_id)
    return (
        _tag(1, 0) + _varint(_METHOD_DELETE)
        + _tag(_METHOD_DELETE, 2) + _varint(len(inner)) + inner
    )


def _record_file_payload(filename: str) -> bytes:
    filename_bytes = filename.encode()
    inner = _tag(1, 2) + _varint(len(filename_bytes)) + filename_bytes
    return (
        _tag(1, 0) + _varint(_METHOD_RECORD_FILE)
        + _tag(_METHOD_RECORD_FILE, 2) + _varint(len(inner)) + inner
    )


# --- Admin session: login once, impersonate per call --------------------------
#
# Login (Step 1) authenticates the *session* by setting an HttpOnly
# RefreshTokenCookie -- that cookie, not a bearer header or a token value in
# the request body, is what /connect/token checks on the impersonate call
# (Step 2). Confirmed against a HAR capture of the real webclient: its
# impersonate request carries no Authorization header and no refresh_token
# body param at all. So the admin identity here is the requests.Session's
# cookie jar, kept alive for the process lifetime, not a string we pass
# around ourselves.

class _AdminSession:
    def __init__(self):
        self._lock = threading.Lock()
        self._session = requests.Session()
        self._logged_in = False
        self._own_access_token = None

    def invalidate(self) -> None:
        """Forces the next impersonate()/login to re-authenticate rather than
        reuse the cached cookie session -- used when a downstream call (e.g.
        opening a MyPhone session) rejects a token the login/impersonate
        handshake itself considered valid."""
        with self._lock:
            self._logged_in = False

    def _login(self) -> None:
        # SecurityCode is 3CX's TOTP field -- required whenever the admin
        # extension has 2FA enrolled, ignored otherwise. Generated fresh per
        # login attempt (not cached) since each code is only valid ~30s.
        security_code = (
            pyotp.TOTP(config.THREECX_ADMIN_TOTP_SECRET).now()
            if config.THREECX_ADMIN_TOTP_SECRET
            else ""
        )
        resp = self._session.post(
            f"{config.THREECX_PBX_URL}{_LOGIN_PATH}",
            json={
                "Username": config.THREECX_ADMIN_EXTENSION,
                "Password": config.THREECX_ADMIN_PASSWORD,
                "SecurityCode": security_code,
                "ReCaptchaResponse": None,
            },
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("Status") != "AuthSuccess":
            raise NotifyError(f"3CX admin login failed: status={data.get('Status')!r}")
        self._own_access_token = data["Token"]["access_token"]
        self._logged_in = True

    def impersonate(self, extension: str) -> str:
        """Returns an access_token scoped to `extension`. Logs in first if
        this is the first call, or the session cookie has gone stale.

        3CX rejects impersonating the admin's own extension with a 403
        (confirmed: works for other extensions, 403s only when
        extension == THREECX_ADMIN_EXTENSION) -- acting on the admin's own
        mailbox just uses the admin's own access_token from login, no
        impersonate call needed."""
        if extension == config.THREECX_ADMIN_EXTENSION:
            with self._lock:
                if not self._logged_in:
                    self._login()
                return self._own_access_token

        last_error = None
        for force_login in (False, True):
            with self._lock:
                if force_login or not self._logged_in:
                    self._login()
            resp = self._session.post(
                f"{config.THREECX_PBX_URL}{_TOKEN_PATH}",
                headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
                data={
                    "client_id": "Webclient",
                    "grant_type": "refresh_token",
                    "impersonate": extension,
                },
                timeout=_TIMEOUT,
            )
            if resp.status_code == 401 and not force_login:
                last_error = NotifyError(f"Impersonation of {extension} was rejected (401)")
                continue
            resp.raise_for_status()
            return resp.json()["access_token"]
        raise last_error or NotifyError(f"Could not impersonate extension {extension}")


_admin = _AdminSession()


def _open_myphone_session(access_token: str) -> str:
    resp = requests.post(
        f"{config.THREECX_PBX_URL}{_SESSION_PATH}",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"name": "Webclient", "version": "20.0.9.0", "isHuman": True},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["sessionKey"]


def _impersonate_and_open_session(extension: str) -> str:
    """impersonate() can hand back a token the session endpoint itself then
    rejects with 401 (the admin cookie session was stale in a way the
    /connect/token exchange didn't catch) -- force one fresh admin login and
    retry before giving up, mirroring impersonate()'s own retry-on-401.
    Wraps failures as NotifyError so callers never see a raw requests
    exception."""
    for attempt in (0, 1):
        access_token = _admin.impersonate(extension)
        try:
            return _open_myphone_session(access_token)
        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 401 and attempt == 0:
                _admin.invalidate()
                continue
            raise NotifyError(f"Could not open MyPhone session for extension {extension}: {e}") from e
    raise NotifyError(f"Could not open MyPhone session for extension {extension}")


def _call_mark_heard_rpc(session_key: str, voicemail_id: int, heard: bool) -> None:
    resp = requests.post(
        f"{config.THREECX_PBX_URL}{_RPC_PATH}",
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/octet-stream",
            "MyPhoneSession": session_key,
        },
        data=_mark_heard_payload(voicemail_id, heard),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    # Response body is a generic ack regardless of success/failure —
    # never treat it as a signal. Caller must verify via the DB.


def _call_delete_rpc(session_key: str, voicemail_id: int) -> None:
    resp = requests.post(
        f"{config.THREECX_PBX_URL}{_RPC_PATH}",
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/octet-stream",
            "MyPhoneSession": session_key,
        },
        data=_delete_payload(voicemail_id),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    # Same generic-ack caveat as _call_mark_heard_rpc — verify via DB.


def _call_record_file_rpc(session_key: str, filename: str) -> None:
    resp = requests.post(
        f"{config.THREECX_PBX_URL}{_RPC_PATH}",
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/octet-stream",
            "MyPhoneSession": session_key,
        },
        data=_record_file_payload(filename),
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    # Same generic-ack caveat as _call_mark_heard_rpc/_call_delete_rpc, plus
    # one more: there's no DB row to verify this against at all. This RPC
    # alone also does not create the file or activate it as the greeting --
    # confirmed empirically (see notify_record_file's docstring) -- it's
    # purely a heads-up to 3CX that a phone-based recording is starting.


def _heard_matches(row: dict, expected: bool) -> bool:
    # s_voicemail.heard is a text column ('0'/'1'/''/NULL), not a real
    # boolean — mirrors routes/mailboxes.py's _is_unheard().
    is_unheard = row is None or row["heard"] is None or row["heard"] in ("", "0")
    return is_unheard != expected


def notify_heard(voicemail_id: int, extension: str, heard: bool) -> None:
    """Impersonate the mailbox-owning `extension` and mark `voicemail_id`
    heard/unheard through 3CX's own MyPhone RPC, so 3CX fires the real SIP
    NOTIFY (MWI) to that extension's phone.

    `extension` must be the mailbox owner (message["callee"]), not the
    viewer performing the action — the RPC is strictly ownership-scoped and
    silently no-ops against any other extension's mailbox even from a full
    admin session (see notes/3cx-native-notify.md).

    Verifies the result against the DB afterward, since the RPC's ack is
    identical whether or not the write actually happened.
    """
    session_key = _impersonate_and_open_session(extension)
    try:
        _call_mark_heard_rpc(session_key, voicemail_id, heard)
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"Mark-heard RPC failed for voicemail {voicemail_id} (ext {extension}): {e}") from e

    row = threecx_db.get_message(voicemail_id)
    if not _heard_matches(row, heard):
        raise NotifyError(
            f"Native notify for voicemail {voicemail_id} (ext {extension}) did not "
            f"take effect (expected heard={heard}); ownership mismatches fail silently."
        )


def notify_delete(voicemail_id: int, extension: str) -> None:
    """Impersonate the mailbox-owning `extension` and delete `voicemail_id`
    through 3CX's own MyPhone RPC (method 126), same ownership-scoping and
    silent-failure caveats as notify_heard above -- see
    notes/3cx-native-notify.md. Unlike heard/unheard, this is a hard delete:
    3CX removes the s_voicemail row outright rather than flipping a flag, and
    there is no undo.
    """
    session_key = _impersonate_and_open_session(extension)
    try:
        _call_delete_rpc(session_key, voicemail_id)
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"Delete RPC failed for voicemail {voicemail_id} (ext {extension}): {e}") from e

    if threecx_db.get_message(voicemail_id) is not None:
        raise NotifyError(
            f"Native delete for voicemail {voicemail_id} (ext {extension}) did not "
            f"take effect; ownership mismatches fail silently."
        )


def notify_record_file(extension: str, filename: str) -> None:
    """Impersonate `extension` and issue 3CX's MyPhone RPC method 144
    ("RecordFile"), reverse-engineered from the 3CX webclient's Settings >
    Greetings "Record Default" flow (see recordmessage-full.har). Unlike
    notify_heard/notify_delete this has no DB row to verify success against,
    and in testing it did not itself create `filename` on disk -- that
    happens as a side effect of the phone call this triggers (3CX calls the
    extension; its owner records live). Once the file exists, activating it
    is a separate step through the xapi greeting-file-manager functions
    below (set_active_greeting_filename), not this RPC.
    """
    session_key = _impersonate_and_open_session(extension)
    try:
        _call_record_file_rpc(session_key, filename)
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"RecordFile RPC failed for extension {extension} (file {filename}): {e}") from e


# --- Greeting file manager (xapi, not the legacy RPC above) -----------------
#
# _admin.impersonate() only retries-on-401 around its own /connect/token
# call (see its docstring) -- a token it hands back can still turn out
# stale by the time it's actually used against xapi (confirmed live: xapi
# returned 401 well after impersonate() itself succeeded). _xapi_request
# below adds the same retry-once-after-invalidate() pattern
# _impersonate_and_open_session already has for the legacy RPC's session
# bootstrap, just generalized across GET/PATCH/DELETE instead of one fixed
# call.

def _xapi_request(extension: str, method: str, url: str, **kwargs) -> requests.Response:
    # Pop headers once, outside the loop -- a dict pop()ed from kwargs
    # inside the loop body would vanish after the first attempt, silently
    # dropping caller-supplied headers (e.g. PATCH's Content-Type) on retry.
    extra_headers = kwargs.pop("headers", {})
    for attempt in (0, 1):
        access_token = _admin.impersonate(extension)
        headers = {"Authorization": f"Bearer {access_token}", "Accept": "application/json", **extra_headers}
        resp = requests.request(method, url, headers=headers, timeout=_TIMEOUT, **kwargs)
        if resp.status_code == 401 and attempt == 0:
            _admin.invalidate()
            continue
        resp.raise_for_status()
        return resp


def list_greeting_files(extension: str) -> list[str]:
    """Every greeting WAV filename 3CX has stored for `extension`, shared
    across all its greeting profiles (Default/Available/Away/Do Not
    Disturb -- a profile just points at one of these, or none at all for
    3CX's own system default)."""
    try:
        resp = _xapi_request(extension, "GET", f"{config.THREECX_PBX_URL}{_XAPI_GREETINGS_PATH}")
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"Could not list greeting files for extension {extension}: {e}") from e
    return [row["Filename"] for row in resp.json().get("value", [])]


def get_active_greeting_filename(extension: str, profile: str = "Default") -> str | None:
    """The filename `profile` currently points at, or None if it has no
    override (playing 3CX's system default)."""
    try:
        resp = _xapi_request(
            extension, "GET", f"{config.THREECX_PBX_URL}{_XAPI_MYUSER_PATH}",
            params={"$select": "Greetings", "$expand": "Greetings"},
        )
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"Could not read greeting profile for extension {extension}: {e}") from e
    for row in resp.json().get("Greetings", []):
        if row.get("Type") == profile:
            return row.get("Filename") or None
    return None


def set_active_greeting_filename(extension: str, filename: str, profile: str = "Default") -> None:
    """Points `profile` at `filename` -- pass "" to clear it back to 3CX's
    system default (what deleting the active file also requires as a
    separate follow-up call, see delete_greeting_file)."""
    try:
        _xapi_request(
            extension, "PATCH", f"{config.THREECX_PBX_URL}{_XAPI_MYUSER_PATH}",
            headers={"Content-Type": "application/json"},
            json={"Greetings": [{"Type": profile, "Filename": filename}]},
        )
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"Could not set greeting profile for extension {extension}: {e}") from e


def delete_greeting_file(extension: str, filename: str) -> None:
    """Permanently deletes one greeting WAV from 3CX's own store. Does NOT
    clear a profile that was pointing at it -- confirmed against the real
    webclient's own delete flow, which issues a separate PATCH afterward
    (see set_active_greeting_filename); callers here must do the same."""
    try:
        _xapi_request(
            extension, "DELETE",
            f"{config.THREECX_PBX_URL}{_XAPI_DELETE_GREETING_PATH}/{quote(filename, safe='')}",
        )
    except requests.exceptions.HTTPError as e:
        raise NotifyError(f"Could not delete greeting file {filename} for extension {extension}: {e}") from e
