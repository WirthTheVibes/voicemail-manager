"""
Daily digest of still-unread voicemails, one email per mailbox that has at
least one. Driven by a systemd timer (vm-manager-daily-digest.timer), not
the FastAPI app process -- see daily_digest.py at the repo root, the actual
entry point this module's main() is called from.

Deliberately reuses access.notification_recipients_for_mailbox as-is (the
same recipient set as the real-time "new voicemail" alert in
notifications.py) rather than a separate recipient rule -- a mailbox's
notify_suppress opt-out already covers anyone who shouldn't hear about it,
for both the real-time alert and this digest.
"""
import logging
import smtplib
from email.message import EmailMessage

from . import access, config, email_template, notifications, threecx_db

logger = logging.getLogger(__name__)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [daily_digest] %(message)s"))
    logger.addHandler(_handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _all_mailbox_extensions() -> set[str]:
    return {row["extension"] for row in threecx_db.directory()} | access.group_mailbox_extensions()


def _send_digest(extension: str, messages: list[dict]) -> None:
    recipient_extensions = access.notification_recipients_for_mailbox(extension)
    to_addrs = sorted({addr for addr in (notifications.resolve_email(ext) for ext in recipient_extensions) if addr})
    if not to_addrs:
        logger.info("No email on file for any recipient of extension %s, skipping digest (%d unread)", extension, len(messages))
        return

    subject, html_body = email_template.build_digest(extension, messages)
    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = config.SMTP_FROM
    email["Bcc"] = ", ".join(to_addrs)
    email.set_content(
        f"You have {len(messages)} unread voicemail(s) in mailbox {extension}.\n\n"
        "Log in to vm-manager to listen."
    )
    email.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
        if config.SMTP_USE_TLS:
            smtp.starttls()
        if config.SMTP_USERNAME and config.SMTP_PASSWORD:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        smtp.send_message(email)
    logger.info("Sent daily digest for extension %s (%d unread) to %d recipient(s)", extension, len(messages), len(to_addrs))


def main() -> None:
    if not config.SMTP_CONFIGURED:
        logger.warning("SMTP_HOST/SMTP_FROM are not set in .env, skipping daily digest run")
        return

    sent, skipped = 0, 0
    for extension in sorted(_all_mailbox_extensions()):
        messages = threecx_db.unread_messages_for_mailbox(extension)
        if not messages:
            continue
        try:
            _send_digest(extension, messages)
            sent += 1
        except Exception:
            logger.exception("Daily digest failed for extension %s", extension)
            skipped += 1
    logger.info("Daily digest run complete: %d sent, %d failed", sent, skipped)


if __name__ == "__main__":
    main()
