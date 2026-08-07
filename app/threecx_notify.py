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

import requests

from . import config, threecx_db

_LOGIN_PATH = "/webclient/api/Login/GetAccessToken"
_TOKEN_PATH = "/connect/token"
_SESSION_PATH = "/webclient/api/MyPhone/session"
_RPC_PATH = "/MyPhone/MPWebService.asmx"
_METHOD_MARK_HEARD = 114
_METHOD_DELETE = 126

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
        resp = self._session.post(
            f"{config.THREECX_PBX_URL}{_LOGIN_PATH}",
            json={
                "Username": config.THREECX_ADMIN_EXTENSION,
                "Password": config.THREECX_ADMIN_PASSWORD,
                "SecurityCode": "",
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
