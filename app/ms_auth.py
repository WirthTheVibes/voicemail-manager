"""Verifies Entra ID (Microsoft) ID tokens for the web UI's MS Auth sign-in.

The browser does the Authorization Code + PKCE exchange itself against
login.microsoftonline.com (see static/login.js and static/callback.js) --
this app never sees a client secret and never talks to Microsoft's token
endpoint. All it does is verify the ID token the browser hands it: check
the signature against Microsoft's own public signing keys (fetched live,
not a shared secret) and check issuer/audience/expiry, exactly like any
JWT-based SSO verification.

Alongside the ID token, the browser also requests a Graph-scoped access
token (delegated User.Read, same PKCE exchange, still no client secret --
see login.js/callback.js) and hands that to us too. fetch_proxy_addresses
uses it to call GET /me?$select=proxyAddresses on the signed-in user's own
behalf, so auth_routes.login_ms can match a 3CX extension against any of a
user's Entra aliases, not just whichever address happened to land in the
ID token's preferred_username/upn claim.
"""
import logging

import jwt
import requests
from jwt import PyJWKClient

from . import config

log = logging.getLogger(__name__)

_ISSUER = f"https://login.microsoftonline.com/{config.MS_AUTH_TENANT_ID}/v2.0"
_JWKS_URL = f"https://login.microsoftonline.com/{config.MS_AUTH_TENANT_ID}/discovery/v2.0/keys"
_GRAPH_ME_URL = "https://graph.microsoft.com/v1.0/me"

# PyJWKClient caches Microsoft's signing keys and re-fetches on a cache miss
# (e.g. after Microsoft rotates them) -- nothing here needs restarting when
# that happens.
_jwk_client = PyJWKClient(_JWKS_URL) if config.MS_AUTH_ENABLED else None


class MsAuthError(Exception):
    pass


def validate_id_token(id_token: str) -> dict:
    """Returns the token's verified claims, or raises MsAuthError."""
    if not config.MS_AUTH_ENABLED:
        raise MsAuthError("Microsoft sign-in isn't configured on this server.")
    try:
        signing_key = _jwk_client.get_signing_key_from_jwt(id_token)
        return jwt.decode(
            id_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=config.MS_AUTH_CLIENT_ID,
            issuer=_ISSUER,
        )
    except jwt.PyJWTError as e:
        raise MsAuthError(f"Invalid Microsoft sign-in token: {e}") from e


def fetch_proxy_addresses(access_token: str) -> list[str]:
    """Returns the signed-in user's own SMTP proxyAddresses (aliases), via a
    delegated Graph call authenticated with the user's own access token --
    nothing here needs app-level Graph permissions or admin consent, since
    User.Read already lets a user read their own directory record.

    Best-effort: login should still succeed on the caller's primary
    UPN/email match without this, so any failure here just yields an empty
    list rather than raising.
    """
    try:
        resp = requests.get(
            _GRAPH_ME_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            params={"$select": "proxyAddresses"},
            timeout=5,
        )
        resp.raise_for_status()
        raw = resp.json().get("proxyAddresses", [])
    except requests.RequestException as e:
        body = getattr(e.response, "text", None) if hasattr(e, "response") else None
        log.warning("Graph proxyAddresses lookup failed: %s (body: %s)", e, body)
        return []
    log.warning("Graph proxyAddresses lookup ok: %r", raw)

    addresses = []
    for entry in raw:
        # Entries look like "SMTP:user@primary.com" (primary, uppercase) or
        # "smtp:user@alias.com" (secondary alias) -- x500/other address
        # types we don't care about here don't have this prefix.
        prefix, _, address = entry.partition(":")
        if prefix.lower() == "smtp" and address:
            addresses.append(address.lower())
    return addresses
