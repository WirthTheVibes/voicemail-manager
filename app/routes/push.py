from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from .. import app_db, config
from ..deps import get_session

router = APIRouter()


@router.get("/api/push/vapid-public-key")
def get_vapid_public_key(session: dict = Depends(get_session)):
    if not config.PUSH_CONFIGURED:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Push notifications are not configured on this server")
    return {"publicKey": config.VAPID_PUBLIC_KEY}


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscriptionRequest(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


@router.post("/api/push/subscribe")
def subscribe(body: PushSubscriptionRequest, session: dict = Depends(get_session)):
    app_db.add_push_subscription(session["extension"], body.endpoint, body.keys.p256dh, body.keys.auth)
    return {"ok": True}


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


@router.delete("/api/push/subscribe")
def unsubscribe(body: PushUnsubscribeRequest, session: dict = Depends(get_session)):
    app_db.remove_push_subscription(body.endpoint)
    return {"ok": True}
