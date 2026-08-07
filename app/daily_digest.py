"""
Daily digest of still-unread voicemails: one combined email per recipient
(not per mailbox) covering every mailbox they're a notification recipient
for that currently has unread voicemail -- someone who's both a personal
mailbox owner and a department mailbox member gets one email with both
sections, not two separate emails. Registered as a scheduler.py job, not
run directly by systemd -- see scheduler.py's module docstring.

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


def _mailboxes_by_recipient() -> dict[str, dict[str, list[dict]]]:
    """recipient_extension -> {mailbox_extension: messages}, with each
    recipient's own personal mailbox (if they have unread mail there)
    ordered first, then any other mailboxes (e.g. a shared department
    mailbox they're a member of) in extension order."""
    unread_by_mailbox = {
        mailbox_ext: messages
        for mailbox_ext in sorted(_all_mailbox_extensions())
        if (messages := threecx_db.unread_messages_for_mailbox(mailbox_ext))
    }

    grouped: dict[str, dict[str, list[dict]]] = {}
    for mailbox_ext, messages in unread_by_mailbox.items():
        for recipient_ext in access.notification_recipients_for_mailbox(mailbox_ext):
            grouped.setdefault(recipient_ext, {})[mailbox_ext] = messages

    ordered: dict[str, dict[str, list[dict]]] = {}
    for recipient_ext, mailboxes in grouped.items():
        if recipient_ext in mailboxes:
            reordered = {recipient_ext: mailboxes[recipient_ext]}
            reordered.update((k, v) for k, v in mailboxes.items() if k != recipient_ext)
            ordered[recipient_ext] = reordered
        else:
            ordered[recipient_ext] = mailboxes
    return ordered


def _send_digest(recipient_extension: str, mailbox_messages: dict[str, list[dict]]) -> None:
    to_addr = notifications.resolve_email(recipient_extension)
    total = sum(len(messages) for messages in mailbox_messages.values())
    if not to_addr:
        logger.info(
            "No email on file for extension %s, skipping digest (%d unread across %d mailbox(es))",
            recipient_extension, total, len(mailbox_messages),
        )
        return

    subject, html_body = email_template.build_digest(recipient_extension, mailbox_messages)
    email = EmailMessage()
    email["Subject"] = subject
    email["From"] = config.SMTP_FROM
    # Bcc even for this single-recipient send, matching notifications.py's
    # convention -- keeps the address out of the To box either way.
    email["Bcc"] = to_addr
    email.set_content(
        f"You have {total} unread voicemail(s) across {len(mailbox_messages)} mailbox(es).\n\n"
        "Log in to vm-manager to listen."
    )
    email.add_alternative(html_body, subtype="html")
    with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10) as smtp:
        if config.SMTP_USE_TLS:
            smtp.starttls()
        if config.SMTP_USERNAME and config.SMTP_PASSWORD:
            smtp.login(config.SMTP_USERNAME, config.SMTP_PASSWORD)
        smtp.send_message(email)
    logger.info(
        "Sent daily digest to extension %s (%d unread across %d mailbox(es))",
        recipient_extension, total, len(mailbox_messages),
    )


def main() -> None:
    if not config.SMTP_CONFIGURED:
        logger.warning("SMTP_HOST/SMTP_FROM are not set in .env, skipping daily digest run")
        return

    sent, skipped = 0, 0
    for recipient_extension, mailbox_messages in sorted(_mailboxes_by_recipient().items()):
        try:
            _send_digest(recipient_extension, mailbox_messages)
            sent += 1
        except Exception:
            logger.exception("Daily digest failed for extension %s", recipient_extension)
            skipped += 1
    logger.info("Daily digest run complete: %d sent, %d failed", sent, skipped)


if __name__ == "__main__":
    main()
