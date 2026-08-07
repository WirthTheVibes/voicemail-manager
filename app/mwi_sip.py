"""
Minimal SIP helper for mwi_relay.py -- raw UDP, digest auth, just enough to
send an unsolicited NOTIFY for the message-summary event package.

Deliberately not pjsua2 (unlike phone_service.py): pjsua2's high-level
Account/Buddy API is built around one AOR you register as and doesn't offer
a way to fire an out-of-dialog NOTIFY toward an arbitrary other extension.
Confirmed live that 3CX accepts an unsolicited NOTIFY routed through its own
proxy toward any extension's AOR and the phone applies it even without ever
subscribing for it (see mwi_relay.py's module docstring for the full
background, including why this ended up polling-based rather than
SUBSCRIBE-based for detecting *changes*). UDP-only: matches this
deployment's PBX_TRANSPORT=udp loopback config (see config.PHONE_TRANSPORT)
-- not built to handle a TCP/TLS transport.
"""
import hashlib
import random
import string


def tag(n=8):
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=n))


def branch():
    return "z9hG4bK" + tag(16)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def digest_response(username, realm, password, method, uri, nonce, qop=None, nc=None, cnonce=None):
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        return _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def parse_headers(raw: str) -> dict:
    headers = {}
    for line in raw.split("\r\n")[1:]:
        if not line or ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers.setdefault(k.strip(), v.strip())
    return headers


def parse_status_code(raw: str) -> str:
    return raw.split("\r\n", 1)[0].split(" ", 2)[1]


def parse_auth_challenge(header_value: str) -> dict:
    parts = {}
    body = header_value.split(" ", 1)[1]
    for chunk in body.split(","):
        chunk = chunk.strip()
        if "=" in chunk:
            k, v = chunk.split("=", 1)
            parts[k.strip()] = v.strip().strip('"')
    return parts


def find_challenge(headers: dict) -> dict | None:
    if "WWW-Authenticate" in headers:
        return {"name": "WWW-Authenticate", "value": headers["WWW-Authenticate"]}
    if "Proxy-Authenticate" in headers:
        return {"name": "Proxy-Authenticate", "value": headers["Proxy-Authenticate"]}
    return None


def build_auth_header(username, password, method, uri, challenge_header) -> tuple[str, str]:
    """Returns (header_name, header_value) for the Authorization/
    Proxy-Authorization response to a 401/407 challenge."""
    challenge = parse_auth_challenge(challenge_header["value"])
    realm = challenge["realm"]
    nonce = challenge["nonce"]
    qop = challenge.get("qop")
    cnonce = tag(16) if qop else None
    nc = "00000001" if qop else None
    response = digest_response(username, realm, password, method, uri, nonce, qop, nc, cnonce)
    parts = [
        f'Digest username="{username}"', f'realm="{realm}"', f'nonce="{nonce}"',
        f'uri="{uri}"', f'response="{response}"', 'algorithm=MD5',
    ]
    if qop:
        parts += [f'qop={qop}', f'nc={nc}', f'cnonce="{cnonce}"']
    header_name = "Authorization" if challenge_header["name"] == "WWW-Authenticate" else "Proxy-Authorization"
    return header_name, ", ".join(parts)


def build_notify(pbx_domain, pbx_ip, local_port, from_ext, target_ext, unread, call_id, cseq, br, from_tag, auth_header=None):
    contact = f"sip:{from_ext}@{pbx_ip}:{local_port}"
    body = f"Messages-Waiting: {'yes' if unread else 'no'}\r\nvoice-message: {unread}/0\r\n"
    lines = [
        f"NOTIFY sip:{target_ext}@{pbx_domain} SIP/2.0",
        f"Via: SIP/2.0/UDP {pbx_ip}:{local_port};branch={br};rport",
        "Max-Forwards: 70",
        f"From: <sip:{from_ext}@{pbx_domain}>;tag={from_tag}",
        f"To: <sip:{target_ext}@{pbx_domain}>",
        f"Call-ID: {call_id}",
        f"CSeq: {cseq} NOTIFY",
        f"Contact: <{contact}>",
        "Event: message-summary",
        "Subscription-State: active;expires=3600",
        "Content-Type: application/simple-message-summary",
        "User-Agent: vm-manager-mwi-relay/1.0",
    ]
    if auth_header:
        lines.append(f"{auth_header[0]}: {auth_header[1]}")
    lines.append(f"Content-Length: {len(body)}")
    return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()
