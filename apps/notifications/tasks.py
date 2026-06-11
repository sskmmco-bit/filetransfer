"""Deferred (bulk) email via Celery (§5.11).

Security-critical / transactional mail is sent inline (apps.notifications.email)
and must never be here. These tasks are fire-and-forget; a few seconds of delay
is fine. Delivery is at-most-once via NotificationLog.idempotency_key, with
retry-and-backoff up to a maximum, after which the row is marked failed and can
be re-enqueued later. notification_sent_at (NotificationLog.sent_at) is set only
on a real SMTP success.
"""
from __future__ import annotations

import logging

from celery import shared_task
from django.utils import timezone

from .email import get_email_connection, render_email, send_now
from .models import EmailType, NotificationLog, NotificationStatus

logger = logging.getLogger(__name__)

MAX_RETRIES = 3


@shared_task(name="apps.notifications.tasks.ping")
def ping() -> str:
    """Trivial task to verify the worker is consuming from Redis."""
    logger.info("notifications.ping: worker is alive")
    return "pong"


# ---------------------------------------------------------------------------
# Generic deferred sender
# ---------------------------------------------------------------------------
@shared_task(bind=True, name="apps.notifications.tasks.send_deferred_email",
             max_retries=MAX_RETRIES, default_retry_delay=60)
def send_deferred_email(self, log_id: int):
    from apps.config.models import SiteSettings

    log = NotificationLog.objects.filter(pk=log_id).first()
    if log is None or log.status == NotificationStatus.SENT:
        return  # already delivered or vanished — idempotent no-op

    bcc = [SiteSettings.get().notification_bcc] if SiteSettings.get().notification_bcc else None
    try:
        send_now(log.subject, log.body, log.recipient, bcc=bcc, connection=get_email_connection())
    except Exception as exc:  # noqa: BLE001
        log.attempts += 1
        log.error = str(exc)[:2000]
        if self.request.retries >= MAX_RETRIES:
            # Give up: mark failed; the key stays so it isn't silently retried,
            # but a later enqueue may reset it to pending and try again.
            log.status = NotificationStatus.FAILED
            log.save(update_fields=["attempts", "error", "status"])
            logger.error("deferred email %s permanently failed: %s", log_id, exc)
            return
        log.status = NotificationStatus.PENDING
        log.save(update_fields=["attempts", "error", "status"])
        raise self.retry(exc=exc, countdown=60 * (self.request.retries + 1))

    log.status = NotificationStatus.SENT
    log.sent_at = timezone.now()
    log.save(update_fields=["status", "sent_at"])

    # Type-specific post-send stamping.
    if log.email_type == EmailType.ASSIGNMENT and log.related_id:
        from apps.files.models import FileAssignment

        FileAssignment.objects.filter(pk=log.related_id).update(notified_at=log.sent_at)


# ---------------------------------------------------------------------------
# Enqueue helpers (called from request/commit paths) — at-most-once
# ---------------------------------------------------------------------------
def _enqueue(key: str, email_type: str, recipient: str, subject: str, body: str,
             related_id: int | None = None) -> None:
    if not recipient:
        return
    log, created = NotificationLog.objects.get_or_create(
        idempotency_key=key,
        defaults={
            "email_type": email_type, "recipient": recipient,
            "subject": subject, "body": body, "related_id": related_id,
        },
    )
    if not created:
        if log.status == NotificationStatus.SENT:
            return  # already delivered — do not re-send
        # Reset a failed/pending row and retry.
        log.status = NotificationStatus.PENDING
        log.subject, log.body, log.related_id = subject, body, related_id
        log.save(update_fields=["status", "subject", "body", "related_id"])
    send_deferred_email.delay(log.id)


def enqueue_assignment_email(assignment) -> None:
    from apps.config.models import SiteSettings

    if not SiteSettings.get().send_assignment_email:
        return
    sf = assignment.stored_file
    subject, body = render_email("file_assignment", {
        "recipient": assignment.recipient, "file": sf, "assigned_by": assignment.assigned_by,
    })
    _enqueue(f"assignment:{assignment.pk}", EmailType.ASSIGNMENT,
             assignment.recipient.email, subject, body, related_id=assignment.pk)


def enqueue_welcome_email(user) -> None:
    from apps.config.models import SiteSettings

    if not SiteSettings.get().send_welcome_email:
        return
    subject, body = render_email("welcome", {"user": user})
    _enqueue(f"welcome:{user.pk}", EmailType.WELCOME, user.email, subject, body)


def enqueue_expiry_reminder(stored_file, recipient_email: str) -> None:
    subject, body = render_email("expiry_reminder", {"file": stored_file})
    # Key includes the expiry date so a renewed expiry can re-notify.
    key = f"expiry:{stored_file.pk}:{stored_file.expiry_date}:{recipient_email}"
    _enqueue(key, EmailType.EXPIRY, recipient_email, subject, body, related_id=stored_file.pk)


# ---------------------------------------------------------------------------
# Scheduled: expiry reminders (Celery Beat)
# ---------------------------------------------------------------------------
@shared_task(name="apps.notifications.tasks.send_expiry_reminders")
def send_expiry_reminders() -> dict:
    """Notify owners/recipients of files expiring within the reminder window."""
    from apps.config.models import SiteSettings
    from apps.files.models import FileStatus, StoredFile

    days = SiteSettings.get().expiry_reminder_days
    target = timezone.localdate() + timezone.timedelta(days=days)
    queued = 0
    files = StoredFile.objects.filter(
        status=FileStatus.ACTIVE, expiry_date=target
    ).select_related("owner").prefetch_related("assignments__recipient")
    for sf in files:
        emails = {sf.owner.email} | {
            a.recipient.email for a in sf.assignments.all() if a.recipient.email
        }
        for email in filter(None, emails):
            enqueue_expiry_reminder(sf, email)
            queued += 1
    logger.info("send_expiry_reminders: window=%s queued=%s", target, queued)
    return {"target_date": str(target), "queued": queued}
