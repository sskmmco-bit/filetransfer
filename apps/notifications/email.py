"""Email plumbing — shared by the transactional (inline) and deferred (Celery)
paths (§5.11).

Both tiers share the same SMTP connection and templates but NOT the same queue:
  * transactional — sent synchronously in the request (a user is waiting).
  * deferred      — handed to Celery (apps.notifications.tasks).

SMTP connection details come from the runtime SiteSettings singleton; when SMTP
is disabled we fall back to Django's configured backend (console in dev).
"""
from __future__ import annotations

import logging

from django.conf import settings
from django.core.mail import EmailMessage, get_connection
from django.template.loader import render_to_string

logger = logging.getLogger(__name__)


def get_email_connection():
    """Build an SMTP connection from SiteSettings, else the default backend."""
    from apps.config.models import SiteSettings

    s = SiteSettings.get()
    if s.smtp_enabled and s.smtp_host:
        # TLS (STARTTLS) and SSL (implicit) are mutually exclusive — Django
        # raises if both are set. Implicit SSL (e.g. port 465) wins.
        use_ssl = bool(s.smtp_use_ssl)
        use_tls = bool(s.smtp_use_tls) and not use_ssl
        return get_connection(
            backend="django.core.mail.backends.smtp.EmailBackend",
            host=s.smtp_host,
            port=s.smtp_port,
            username=s.smtp_username or None,
            password=s.get_smtp_password() or None,
            use_tls=use_tls,
            use_ssl=use_ssl,
        )
    return get_connection()


def from_email() -> str:
    from apps.config.models import SiteSettings

    return SiteSettings.get().default_from_email or settings.DEFAULT_FROM_EMAIL


def render_email(template_name: str, context: dict) -> tuple[str, str]:
    """Render a text email template; first line is the subject, rest is body.

    Template convention: line 1 = "Subject: ...", a blank line, then the body.
    """
    rendered = render_to_string(f"email/{template_name}.txt", context).strip("\n")
    lines = rendered.split("\n")
    subject = lines[0].removeprefix("Subject:").strip()
    body = "\n".join(lines[1:]).strip("\n")
    return subject, body


def send_now(subject: str, body: str, recipient: str, *, bcc=None, connection=None) -> bool:
    """Send a single message immediately. Returns True on success."""
    conn = connection or get_email_connection()
    msg = EmailMessage(
        subject=subject, body=body, from_email=from_email(),
        to=[recipient], bcc=list(bcc) if bcc else None, connection=conn,
    )
    msg.send(fail_silently=False)
    return True


# ----- Transactional (inline) — never queued -----
def send_transactional_email(subject: str, message: str, recipient: str) -> bool:
    try:
        return send_now(subject, message, recipient)
    except Exception as exc:  # noqa: BLE001
        logger.error("transactional email to %s failed: %s", recipient, exc)
        return False


def send_public_verification_code(email: str, file_name: str, code: str) -> bool:
    subject, body = render_email(
        "public_verification_code", {"file_name": file_name, "code": code}
    )
    return send_transactional_email(subject, body, email)
