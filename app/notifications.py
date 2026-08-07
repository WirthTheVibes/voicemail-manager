"""
Best-effort delivery of "new voicemail" notifications via SMTP and/or Web
Push, gated independently by Settings > Notifications
(app_db.get/set_notification_settings).

Registered as a voicemail_watcher handler (see main.py's lifespan) -- runs
on the watcher's own dedicated thread, so a slow SMTP server or push
endpoint delays the next poll tick, never the app itself (same isolation
reasoning as phone_service.py/transcription_worker.py: this thread is
neither the asyncio event loop nor the request threadpool).

Delivery failures are logged and dropped, not retried -- see
voicemail_watcher.py's module docstring for why persistent retry state
isn't worth it here: the voicemail itself is never lost, it's still
sitting in the mailbox regardless of whether anyone got pinged about it.

Deliberately never touches s_voicemail's own notification_sent/
notify_failed/attempts/lasterror columns -- those belong to 3CX's own
(unused, presumably license-gated) native notify feature, and writing to
them risks tripping whatever reads them on 3CX's side.
"""
import json
import logging
import smtplib
from email.message import EmailMessage

from . import access, app_db, config, email_template, threecx_db

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [notify] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False

try:
    from pywebpush import WebPushException, webpush
except ImportError:
    webpush = None
    WebPushException = Exception


def resolve_email(extension: str) -> str | None:
    """app_db's app_user cache first (may be a manual override); falls back
    to 3CX's own voicemail.email and caches it for next time -- see
    app_db.sync_user_email_from_3cx for why that never clobbers a manual
    override."""
    email = app_db.get_user_email(extension)
    if email:
        return email
    row = threecx_db.mailbox_email(extension)
    if row and row["email"]:
        app_db.sync_user_email_from_3cx(extension, row["email"])
        return row["email"]
    return None


def _send_smtp(message: dict) -> None:
    extension = message["callee"]
    recipient_extensions = access.notification_recipients_for_mailbox(extension)
    to_addrs = sorted({addr for addr in (resolve_email(ext) for ext in recipient_extensions) if addr})
    if not to_addrs:
        logger.info("No email on file for any recipient of extension %s, skipping SMTP notify (message %s)", extension, message["id"])
        return
    if not config.SMTP_CONFIGURED:
        logger.warning("SMTP notifications enabled but SMTP_HOST/SMTP_FROM are not set in .env")
        return

    subject, html_body = email_template.build(message)
    caller = message.get("caller_name") or message.get("caller") or "Unknown caller"
    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = config.SMTP_FROM
    # Every recipient goes in Bcc only -- including the single-recipient
    # case -- so nobody's address (and not the sender's own) shows up in
    # the To box.
    email["Bcc"] = ", ".join(to_addrs)
    email.set_content(
        f"New voicemail for extension {extension} from {caller}.\n"
        f"Duration: {message.get('duration')}s\n"
        f"Received: {message.get('created_time')}\n\n"
        "Log in to vm-manager to listen."
    )
    email.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
        if config.SMTP_USE_TLS:
            smtp.starttls()
        if config.SMTP_USERNAME and config.SMTP_PASSWORD:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        smtp.send_message(email)
    logger.info("Sent SMTP notification for message %s to %d recipient(s)", message["id"], len(to_addrs))


def _send_push(message: dict) -> None:
    if webpush is None:
        logger.warning("PWA notifications enabled but pywebpush is not installed")
        return
    if not config.PUSH_CONFIGURED:
        logger.warning("PWA notifications enabled but VAPID_PUBLIC_KEY/VAPID_PRIVATE_KEY/VAPID_CONTACT_EMAIL are not set in .env")
        return

    extension = message["callee"]
    subs = [
        sub
        for recipient_ext in access.notification_recipients_for_mailbox(extension)
        for sub in app_db.push_subscriptions_for_extension(recipient_ext)
    ]
    if not subs:
        return

    caller = message.get("caller_name") or message.get("caller") or "Unknown caller"
    payload = json.dumps({"title": "New voicemail", "body": f"From {caller}", "messageId": message["id"]})

    sent = 0
    for sub in subs:
        try:
            webpush(
                subscription_info={
                    "endpoint": sub["endpoint"],
                    "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]},
                },
                data=payload,
                vapid_private_key=config.VAPID_PRIVATE_KEY,
                vapid_claims={"sub": f"mailto:{config.VAPID_CONTACT_EMAIL}"},
            )
            sent += 1
        except WebPushException as e:
            # 404/410 means the browser itself dropped this subscription
            # (uninstalled, cleared site data, etc.) -- expected to happen
            # over time, so garbage collect it instead of logging it as a
            # real failure on every future message.
            status = e.response.status_code if e.response is not None else None
            if status in (404, 410):
                app_db.remove_push_subscription(sub["endpoint"])
            else:
                logger.warning("Push failed for extension %s: %s", extension, e)
        except Exception:
            logger.exception("Push failed for extension %s", extension)
    if sent:
        logger.info("Sent push notification for message %s to %d subscription(s)", message["id"], sent)


def dispatch(message: dict) -> None:
    """voicemail_watcher handler: fires whichever channels are enabled in
    Settings > Notifications for this newly-arrived message. Each channel
    is independent -- SMTP failing never blocks push and vice versa."""
    settings = app_db.get_notification_settings()
    if settings["smtp_enabled"]:
        try:
            _send_smtp(message)
        except Exception:
            logger.exception("SMTP notification failed for message %s", message["id"])
    if settings["pwa_enabled"]:
        try:
            _send_push(message)
        except Exception:
            logger.exception("Push notification failed for message %s", message["id"])
