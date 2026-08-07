"""Verifies Entra ID (Microsoft) ID tokens for the web UI's MS Auth sign-in.

The browser does the Authorization Code + PKCE exchange itself against
login.microsoftonline.com (see static/login.js and static/callback.js) --
this app never sees a client secret and never talks to Microsoft's token
endpoint. All it does is verify the ID token the browser hands it: check
the signature against Microsoft's own public signing keys (fetched live,
not a shared secret) and check issuer/audience/expiry, exactly like any
JWT-based SSO verification.
"""
import jwt
from jwt import PyJWKClient

from . import config

_ISSUER = f"https://login.microsoftonline.com/{config.MS_AUTH_TENANT_ID}/v2.0"
_JWKS_URL = f"https://login.microsoftonline.com/{config.MS_AUTH_TENANT_ID}/discovery/v2.0/keys"

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
