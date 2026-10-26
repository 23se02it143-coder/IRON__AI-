"""Email delivery. Every message is recorded in the `email_outbox` table.

* If SMTP_HOST is set, the message is sent over SMTP (STARTTLS/SSL).
* Otherwise it is only logged and stored (status "logged") - perfect for local development:
  open the admin panel > Emails to read what would have been sent.
"""
from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, parseaddr

from ..config import Settings, get_settings
from ..database import SessionLocal, utcnow
from ..models import EmailOutbox

log = logging.getLogger("iron.mail")


def send_email(
    to: str,
    subject: str,
    text: str,
    html: str = "",
    kind: str = "",
    settings: Settings | None = None,
) -> None:
    """Record and deliver one email. Never raises: failures are stored on the outbox row."""
    settings = settings or get_settings()
    with SessionLocal() as db:
        row = EmailOutbox(to_address=to, subject=subject[:200], body_text=text, body_html=html, kind=kind)
        db.add(row)
        db.commit()

        if not settings.smtp_host:
            row.status = "logged"
            db.commit()
            log.info("Email logged (no SMTP configured): to=%s subject=%s", to, subject)
            return

        try:
            _smtp_send(settings, to, subject, text, html)
            row.status, row.sent_at = "sent", utcnow()
        except Exception as exc:  # noqa: BLE001 - deliberately broad; we record and move on
            row.status, row.error = "failed", str(exc)[:300]
            log.exception("Email delivery failed: to=%s subject=%s", to, subject)
        db.commit()


def _smtp_send(settings: Settings, to: str, subject: str, text: str, html: str) -> None:
    name, address = parseaddr(settings.email_from)
    msg = EmailMessage()
    msg["From"] = formataddr((name, address)) if name else address
    msg["To"] = to
    msg["Subject"] = subject
    msg.set_content(text)
    if html:
        msg.add_alternative(html, subtype="html")

    context = ssl.create_default_context()
    if settings.smtp_security == "ssl":
        server: smtplib.SMTP = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=15, context=context)
    else:
        server = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=15)
        if settings.smtp_security == "starttls":
            server.starttls(context=context)
    try:
        if settings.smtp_user:
            server.login(settings.smtp_user, settings.smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except smtplib.SMTPException:  # pragma: no cover
            pass
