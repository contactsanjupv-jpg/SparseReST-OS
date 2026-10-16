"""Internal (founder-facing) email: growth reports and alerts, sent through the same SMTP mailbox. These are NOT
prospect outreach, so they bypass the outbound governor and ledger on purpose -- a report must never be blocked by,
or counted against, the outreach cap."""
from __future__ import annotations

import smtplib
from email.message import EmailMessage

from packages.core.config import Settings


def can_send_internal(settings: Settings) -> bool:
    return bool(settings.report_email_to and settings.smtp_host and settings.smtp_username and settings.smtp_password and settings.smtp_from_email)


def send_internal_email(settings: Settings, subject: str, body: str, smtp_factory=None) -> None:
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, settings.smtp_from_email, settings.report_email_to
    msg.set_content(body)
    smtp = (smtp_factory or (lambda h, p: smtplib.SMTP(h, p, timeout=20)))(settings.smtp_host, settings.smtp_port)
    try:
        from packages.core.tlsconfig import client_context
        smtp.starttls(context=client_context(settings))
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(msg)
    finally:
        try:
            smtp.quit()
        except Exception:
            pass              # best-effort cleanup: never mask the real error
