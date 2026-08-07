from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel

from .. import app_db, auth, config, ms_auth, threecx_db
from ..deps import get_session

router = APIRouter()


class LoginRequest(BaseModel):
    extension: str
    pin: str


class MsLoginRequest(BaseModel):
    id_token: str


def _start_session(response: Response, user: dict) -> dict:
    """Shared by PIN and MS Auth login: builds the session cookie from a
    user row that has extension/iduser/firstname/lastname, and returns the
    same body shape both login endpoints send back to the client."""
    dept_role = threecx_db.department_and_role(user["extension"])
    role_flags = auth.derive_role_flags(dept_role["roletag"] if dept_role else None)

    session_payload = {
        "extension": user["extension"],
        "iduser": user["iduser"],
        "firstname": user["firstname"],
        "lastname": user["lastname"],
        "is_admin": app_db.is_admin(user["extension"]),
        "department": dept_role["department"] if dept_role else None,
        **role_flags,
    }
    token = auth.create_session_token(session_payload)
    response.set_cookie(
        key=config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )
    return {
        "extension": session_payload["extension"],
        "firstname": session_payload["firstname"],
        "lastname": session_payload["lastname"],
        "is_admin": session_payload["is_admin"],
    }


@router.get("/api/auth-config")
def auth_config():
    # client_id/tenant_id aren't secrets -- they're the same values baked
    # into any public OAuth client (identical to what MSAL.js would embed
    # in the page itself), so it's fine for an unauthenticated request to
    # read them.
    return {
        "ms_auth_enabled": config.MS_AUTH_ENABLED,
        "ms_tenant_id": config.MS_AUTH_TENANT_ID,
        "ms_client_id": config.MS_AUTH_CLIENT_ID,
        "pin_login_enabled": config.PIN_LOGIN_ENABLED,
    }


@router.post("/api/login")
def login(body: LoginRequest, response: Response):
    if not config.PIN_LOGIN_ENABLED:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="PIN sign-in is disabled. Use Microsoft sign-in.",
        )

    if app_db.is_locked_out(body.extension):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Too many failed attempts. Try again in {config.LOGIN_LOCKOUT_MINUTES} minutes.",
        )

    user = threecx_db.authenticate(body.extension, body.pin)
    app_db.record_login_attempt(body.extension, ok=user is not None)

    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid extension or PIN")

    return _start_session(response, user)


@router.post("/api/login/ms")
def login_ms(body: MsLoginRequest, response: Response):
    try:
        claims = ms_auth.validate_id_token(body.id_token)
    except ms_auth.MsAuthError as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(e))

    email = claims.get("preferred_username") or claims.get("upn") or claims.get("email")
    if not email:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That Microsoft account has no usable email/UPN claim.",
        )

    if config.THREECX_ADMIN_EXTENSION and email.lower() in config.MS_AUTH_OVERRIDE_EMAILS:
        user = threecx_db.by_extension(config.THREECX_ADMIN_EXTENSION)
    else:
        user = threecx_db.by_email(email)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"No 3CX extension is linked to {email}.",
        )

    return _start_session(response, user)


@router.post("/api/logout")
def logout(response: Response, session: dict = Depends(get_session)):
    response.delete_cookie(config.SESSION_COOKIE_NAME)
    return {"ok": True}


@router.get("/api/me")
def me(session: dict = Depends(get_session)):
    return {
        "extension": session["extension"],
        "firstname": session.get("firstname"),
        "lastname": session.get("lastname"),
        "is_admin": session.get("is_admin", False),
        "is_supervisor": session.get("is_supervisor", False),
        "is_manager": session.get("is_manager", False),
        "is_system_owner": session.get("is_system_owner", False),
        "department": session.get("department"),
    }
