from fastapi import Cookie, Depends, HTTPException, status

from . import access, app_db, auth, config


def get_session(vm_session: str | None = Cookie(default=None, alias=config.SESSION_COOKIE_NAME)) -> dict:
    if not vm_session:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")
    session = auth.verify_session_token(vm_session)
    if session is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Session expired")

    # The database is always the source of truth for identity/display data —
    # never trust the copies baked into the token at login time. Name,
    # department, role, and admin status can all change mid-session.
    session.update(access.current_identity(session["extension"]))
    session["is_admin"] = app_db.is_admin(session["extension"])
    return session


def require_admin(session: dict = Depends(get_session)) -> dict:
    if not app_db.is_admin(session["extension"]):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    return session
