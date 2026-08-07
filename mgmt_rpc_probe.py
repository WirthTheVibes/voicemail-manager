#!/usr/bin/env python3
"""
Standalone probe for exploring 3CX's legacy MyPhone RPC surface
(MPWebService.asmx) beyond the two methods threecx_notify.py already
implements (114 mark-heard, 126 delete) -- see notes/3cx-native-notify.md
for the auth chain and wire format this follows.

Zero imports from app/ on purpose (same reasoning as the old
dial_and_play.py): this pokes at RPC method IDs that haven't been
reverse-engineered yet, so it must not be able to affect the running
vm-manager service regardless of what it sends or how the PBX responds.

Auth mirrors threecx_notify.py's _AdminSession, including SecurityCode
(TOTP) support for an admin extension with 2FA enrolled.

Usage:
    python3 mgmt_rpc_probe.py --method 114 --field 1:6 --field 2:1
    python3 mgmt_rpc_probe.py --method 105 --field 1:vmail_900_301.wav --impersonate 301

Fields are `fieldnum:value` pairs, in order, sent as the RPC payload.
A value that parses as an int is sent as a varint (wire type 0); anything
else is sent as a length-delimited string (wire type 2) -- covers every
known method so far (114/126 take ints, 105 takes a filename string).

Prints the raw response as hex plus any printable ASCII runs, since
success/failure can't be inferred from the response shape (see notes doc)
-- verify any real effect against the DB or 3CX admin console yourself.
"""
import argparse
import os
import re
import sys
from pathlib import Path

import pyotp
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent / ".env")

PBX_URL = os.environ["THREECX_PBX_URL"].rstrip("/")
ADMIN_EXTENSION = os.environ["THREECX_ADMIN_EXTENSION"]
ADMIN_PASSWORD = os.environ["THREECX_ADMIN_PASSWORD"]
TOTP_SECRET = os.environ.get("THREECX_ADMIN_TOTP_SECRET", "")

_LOGIN_PATH = "/webclient/api/Login/GetAccessToken"
_TOKEN_PATH = "/connect/token"
_SESSION_PATH = "/webclient/api/MyPhone/session"
_RPC_PATH = "/MyPhone/MPWebService.asmx"
_TIMEOUT = 10


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


def build_payload(method_id: int, fields: list[tuple[int, str]]) -> bytes:
    inner = b""
    for field_num, raw_value in fields:
        try:
            inner += _tag(field_num, 0) + _varint(int(raw_value))
        except ValueError:
            value_bytes = raw_value.encode()
            inner += _tag(field_num, 2) + _varint(len(value_bytes)) + value_bytes
    return (
        _tag(1, 0) + _varint(method_id)
        + _tag(method_id, 2) + _varint(len(inner)) + inner
    )


def login(session: requests.Session) -> str:
    """Logs in and returns the admin's own access_token (used directly when
    acting as the admin extension itself -- 3CX 403s an impersonate call
    targeting the admin's own extension, same as threecx_notify.py)."""
    security_code = pyotp.TOTP(TOTP_SECRET).now() if TOTP_SECRET else ""
    resp = session.post(
        f"{PBX_URL}{_LOGIN_PATH}",
        json={
            "Username": ADMIN_EXTENSION,
            "Password": ADMIN_PASSWORD,
            "SecurityCode": security_code,
            "ReCaptchaResponse": None,
        },
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("Status") != "AuthSuccess":
        sys.exit(f"3CX admin login failed: status={data.get('Status')!r}")
    print(f"Logged in as {ADMIN_EXTENSION} (2FA={'on' if TOTP_SECRET else 'off'})")
    return data["Token"]["access_token"]


def get_access_token(session: requests.Session, extension: str, own_access_token: str) -> str:
    if extension == ADMIN_EXTENSION:
        return own_access_token
    resp = session.post(
        f"{PBX_URL}{_TOKEN_PATH}",
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
        data={"client_id": "Webclient", "grant_type": "refresh_token", "impersonate": extension},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def open_session(session: requests.Session, access_token: str) -> str:
    resp = session.post(
        f"{PBX_URL}{_SESSION_PATH}",
        headers={"Authorization": f"Bearer {access_token}"},
        json={"name": "Webclient", "version": "20.0.9.0", "isHuman": True},
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()["sessionKey"]


def call_rpc(session: requests.Session, session_key: str, payload: bytes) -> bytes:
    resp = session.post(
        f"{PBX_URL}{_RPC_PATH}",
        headers={
            "Content-Type": "application/octet-stream",
            "Accept": "application/octet-stream",
            "MyPhoneSession": session_key,
        },
        data=payload,
        timeout=_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.content


def dump(data: bytes) -> None:
    print(f"--- {len(data)} bytes ---")
    print(data.hex(" "))
    strings = re.findall(rb"[\x20-\x7e]{4,}", data)
    if strings:
        print("printable runs:", [s.decode() for s in strings])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--method", type=int, required=True, help="RPC method ID, e.g. 114")
    parser.add_argument(
        "--field", action="append", default=[], metavar="NUM:VALUE",
        help="Payload field as fieldnum:value (repeatable, order matters). "
             "Int-parseable values go as varint, else as a string.",
    )
    parser.add_argument(
        "--impersonate", metavar="EXTENSION",
        help="Extension to impersonate (default: act as the admin extension itself)",
    )
    args = parser.parse_args()

    fields = []
    for raw in args.field:
        num, _, value = raw.partition(":")
        fields.append((int(num), value))

    session = requests.Session()
    own_access_token = login(session)
    extension = args.impersonate or ADMIN_EXTENSION
    access_token = get_access_token(session, extension, own_access_token)
    session_key = open_session(session, access_token)
    print(f"Session open for {extension}: {session_key}")

    payload = build_payload(args.method, fields)
    print("payload:", payload.hex(" "))
    dump(call_rpc(session, session_key, payload))


if __name__ == "__main__":
    main()
