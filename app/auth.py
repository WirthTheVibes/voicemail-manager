import re

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from . import config

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="vm-manager-session")

# Separate serializer/salt from the web cookie session above -- different
# trust boundary. This token rides in a URL query string on every phone
# screen link (Yealink's XML Browser has no cookies/POST body -- see
# notes/README.md), not an HttpOnly cookie, so it gets its own short
# max_age (config.YEALINK_TOKEN_MAX_AGE_SECONDS) independent of the web
# session's.
_yealink_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="vm-manager-yealink")

SUPERVISOR_RE = re.compile(r'name="supervisors"', re.IGNORECASE)
MANAGER_RE = re.compile(r'name="managers"', re.IGNORECASE)
SYSTEM_OWNER_RE = re.compile(r'name="system_owners"', re.IGNORECASE)


def create_session_token(payload: dict) -> str:
    return _serializer.dumps(payload)


def verify_session_token(token: str):
    try:
        return _serializer.loads(token, max_age=config.SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None


def create_yealink_token(extension: str) -> str:
    return _yealink_serializer.dumps({"extension": extension})


def verify_yealink_token(token: str | None) -> str | None:
    """Returns the authenticated extension, or None if the token is
    missing/invalid/expired. The extension embedded here -- never a
    request's own `ext` query param -- is what every Yealink route treats
    as the authenticated identity (see routes/yealink.py)."""
    if not token:
        return None
    try:
        payload = _yealink_serializer.loads(token, max_age=config.YEALINK_TOKEN_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return payload.get("extension")


def derive_role_flags(roletag: str | None) -> dict:
    roletag = roletag or ""
    return {
        "is_supervisor": bool(SUPERVISOR_RE.search(roletag)),
        "is_manager": bool(MANAGER_RE.search(roletag)),
        # System Owner (3CX's top-level role) gets the same org-wide
        # mailbox access as Manager — see access.accessible_mailboxes.
        "is_system_owner": bool(SYSTEM_OWNER_RE.search(roletag)),
    }
