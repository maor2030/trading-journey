"""Email alerts (SMTP), e.g. when the Anthropic API key runs out of credit."""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import Settings

logger = logging.getLogger(__name__)


def is_credit_error(exc: object) -> bool:
    """Anthropic rejects every request with this once the prepaid credit is used up."""
    return isinstance(exc, Exception) and "credit balance is too low" in str(exc).lower()


def email_configured(settings: Settings) -> bool:
    return bool(settings.smtp_user and settings.smtp_password and settings.alert_email)


def send_email(settings: Settings, subject: str, body: str) -> bool:
    """Send one email; False (logged) when SMTP isn't configured or sending fails."""
    if not email_configured(settings):
        logger.warning("Email alert not sent (set SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL): %s", subject)
        return False
    msg = EmailMessage()
    msg["Subject"] = f"[trading-journey] {subject}"
    msg["From"] = settings.smtp_user
    msg["To"] = settings.alert_email
    msg.set_content(body)
    try:
        with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=30,
                              context=ssl.create_default_context()) as smtp:
            smtp.login(settings.smtp_user, settings.smtp_password)
            smtp.send_message(msg)
    except Exception:
        logger.exception("Email alert failed: %s", subject)
        return False
    logger.info("Email alert sent to %s: %s", settings.alert_email, subject)
    return True
