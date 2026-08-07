#!/usr/bin/env python3
"""
Minimal root-privileged helper: sniffs raw SIP traffic on eth0 for final
non-2xx responses from the callee's own phone (486 Busy Here -- rejected,
487 Request Terminated -- rang with no answer and got canceled, 480/600/603
-- other busy/decline variants) and keeps the last REJECT_WINDOW seconds of
them in memory, keyed by the extension in the response's own To: header
(i.e. the phone that actually sent it, not whoever placed the call).

Exists because 3CX transparently reroutes a rejected/no-answer call to its
own internal voicemail app and reports a clean 200 OK back to whatever SIP
client called that extension (see phone_service.py's
_answered_by_pbx_internally docstring) -- the real 486 is only ever visible
on the wire between 3CX's core and the callee's phone, never in the calling
client's own SIP dialog. Running on the PBX box itself means this can watch
that wire directly instead of only inferring the outcome.

Runs as its own systemd unit (sip-reject-watch.service) with only
CAP_NET_RAW -- vm-manager.service (the FastAPI app) stays fully
unprivileged and just queries this over a Unix socket.
"""
import os
import re
import socket
import struct
import threading
import time

IFACE = "eth0"
SOCK_PATH = "/run/sip-reject-watch/query.sock"
REJECT_WINDOW = 10.0
REJECT_CODES = {"486", "487", "480", "600", "603"}

_lock = threading.Lock()
_recent: list[tuple[str, str, str, float]] = []  # (extension, code, reason, ts)

STATUS_RE = re.compile(rb"^SIP/2\.0 (\d{3}) (.+?)\r?$", re.MULTILINE)
TO_RE = re.compile(rb"^To:.*?[Ss]ip:(\d+)@", re.MULTILINE)


def _prune(now: float) -> None:
    cutoff = now - REJECT_WINDOW
    while _recent and _recent[0][3] < cutoff:
        _recent.pop(0)


def _handle_payload(payload: bytes) -> None:
    if not payload.startswith(b"SIP/2.0 "):
        return
    m = STATUS_RE.match(payload)
    if not m or m.group(1).decode() not in REJECT_CODES:
        return
    to_m = TO_RE.search(payload)
    if not to_m:
        return
    extension = to_m.group(1).decode()
    code = m.group(1).decode()
    reason = m.group(2).decode(errors="replace")
    now = time.time()
    with _lock:
        _recent.append((extension, code, reason, now))
        _prune(now)


def _sniff_loop() -> None:
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(0x0003))
    s.bind((IFACE, 0))
    while True:
        packet = s.recv(65535)
        if packet[12:14] != b"\x08\x00":  # IPv4 only
            continue
        ip_start = 14
        ihl = (packet[ip_start] & 0x0F) * 4
        proto = packet[ip_start + 9]
        l4_start = ip_start + ihl
        if proto == 17:  # UDP
            header_len = 8
        elif proto == 6:  # TCP -- 3CX<->phone SIP here runs over TCP, not UDP
            header_len = ((packet[l4_start + 12] >> 4) & 0x0F) * 4
        else:
            continue
        src_port, dst_port = struct.unpack("!HH", packet[l4_start:l4_start + 4])
        if src_port != 5060 and dst_port != 5060:
            continue
        payload = packet[l4_start + header_len:]
        _handle_payload(payload)


def _serve_query(conn: socket.socket) -> None:
    """Query format: "<extension> <since_ts>" -- since_ts is the querying
    call's own start time, so a rejection left over from a *previous* call
    attempt to the same extension (still inside REJECT_WINDOW) never gets
    reattributed to a fresh attempt; only a rejection at or after since_ts
    counts."""
    with conn:
        data = conn.recv(64).strip()
        if not data:
            return
        parts = data.decode(errors="replace").split()
        extension = parts[0]
        since_ts = float(parts[1]) if len(parts) > 1 else 0.0
        now = time.time()
        with _lock:
            _prune(now)
            match = next(
                (r for r in reversed(_recent) if r[0] == extension and r[3] >= since_ts),
                None,
            )
        if match:
            _, code, reason, ts = match
            conn.sendall(f"{code} {reason} {now - ts:.1f}\n".encode())
        else:
            conn.sendall(b"NONE\n")


def _serve_loop() -> None:
    os.makedirs(os.path.dirname(SOCK_PATH), exist_ok=True)
    if os.path.exists(SOCK_PATH):
        os.remove(SOCK_PATH)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(SOCK_PATH)
    os.chmod(SOCK_PATH, 0o660)
    srv.listen(8)
    while True:
        conn, _ = srv.accept()
        threading.Thread(target=_serve_query, args=(conn,), daemon=True).start()


def main() -> None:
    threading.Thread(target=_sniff_loop, daemon=True).start()
    _serve_loop()


if __name__ == "__main__":
    main()
