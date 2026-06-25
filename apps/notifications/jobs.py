"""Deferred (bulk) email — DB-backed queue drained by a management command (§5.11).

Replaces the former Celery path. Security-critical / transactional mail is sent
inline (apps.notifications.email) and must never be here. These jobs are
fire-and-forget; a minute or two of delay (the timer interval) is fine.

Delivery is at-most-once via NotificationLog.idempotency_key, with fixed-interval
retry: a failed send goes back to PENDING and is retried on the next drain, up to
MAX_RETRIES, after which the row is marked FAILED (re-enqueueing resets it).
notification_sent_at (NotificationLog.sent_at) is set only on a real SMTP success.

Draining is overlap-safe: a row is CLAIMED under a short row lock
(select_for_update(skip_locked=True) -> status SENDING + claimed_at), then sent
OUTSIDE the lock. A row stuck in SENDING past NOTIFICATION_CLAIM_STALE_MINUTES
(a crashed run) is re-claimed on a later pass. That stale bound MUST exceed the
SMTP socket timeout (email.get_email_connection) so a slow-but-live send is never
re-claimed and double-sent.
"""
from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .email import get_email_connection, render_email, send_now
from .models import EmailType, NotificationLog, NotificationStatus

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
# Rows in SENDING older than this are assumed abandoned by a crashed run and may
# be re-claimed. MUST be larger than the SMTP socket timeout in email.py.
NOTIFICATION_CLAIM_STALE_MINUTES = 15
# Rows processed per drain run.
DRAIN_BATCH = 100


# ---------------------------------------------------------------------------
# Drain (called by the send_queued_notifications management command)
# ---------------------------------------------------------------------------
def _claim_batch(limit: int) -> list[int]:
    """Atomically claim up to `limit` sendable rows; return their ids.

    Claimable = PENDING, or SENDING whose claim has gone stale (crashed run).
    The lock is held only for the claim; SMTP happens afterwards in _send_claimed.
    """
    stale_cutoff = timezone.now() - timezone.timedelta(
        minutes=NOTIFICATION_CLAIM_STALE_MINUTES
    )
    with transaction.atomic():
        rows = list(
            NotificationLog.objects.select_for_update(skip_locked=True)
            .filter(
                Q(status=NotificationStatus.PENDING)
                | Q(status=NotificationStatus.SENDING, claimed_at__lt=stale_cutoff)
            )
            .order_by("created_at")[:limit]
        )
        now = timezone.now()
        for log in rows:
            log.status = NotificationStatus.SENDING
            log.claimed_at = now
            log.save(update_fields=["status", "claimed_at"])
        return [log.id for log in rows]


def _send_claimed(log_id: int) -> bool | None:
    """Send one claimed row over SMTP (no lock held). True=sent, False=failed,
    None=row vanished or was re-claimed elsewhere."""
    from apps.config.models import SiteSettings

    log = NotificationLog.objects.filter(pk=log_id).first()
    if log is None or log.status != NotificationStatus.SENDING:
        return None

    bcc = [SiteSettings.get().notification_bcc] if SiteSettings.get().notification_bcc else None
    try:
        send_now(log.subject, log.body, log.recipient, bcc=bcc, connection=get_email_connection())
    except Exception as exc:  # noqa: BLE001
        log.attempts += 1
        log.error = str(exc)[:2000]
        # Retry on the next drain until we exhaust MAX_RETRIES, then give up.
        log.status = (
            NotificationStatus.FAILED
            if log.attempts >= MAX_RETRIES
            else NotificationStatus.PENDING
        )
        log.claimed_at = None
        log.save(update_fields=["attempts", "error", "status", "claimed_at"])
        logger.warning("deferred email %s failed (attempt %s): %s", log_id, log.attempts, exc)
        return False

    log.status = NotificationStatus.SENT
    log.sent_at = timezone.now()
    log.claimed_at = None
    log.save(update_fields=["status", "sent_at", "claimed_at"])

    # Type-specific post-send stamping.
    if log.email_type == EmailType.ASSIGNMENT and log.related_id:
        from apps.files.models import FileAssignment

        FileAssignment.objects.filter(pk=log.related_id).update(notified_at=log.sent_at)
    return True


def drain_pending(limit: int = DRAIN_BATCH) -> dict:
    """Claim and send a batch of pending notifications. Writes a CronLog row when
    anything was processed (empty runs stay silent to avoid flooding the log)."""
    started = timezone.now()
    ids = _claim_batch(limit)
    sent = failed = 0
    for log_id in ids:
        result = _send_claimed(log_id)
        if result is True:
            sent += 1
        elif result is False:
            failed += 1

    if ids:
        from apps.audit.models import CronLog

        CronLog.objects.create(
            task_name="send_queued_notifications",
            processed_count=len(ids),
            deleted_count=sent,  # repurposed as "delivered" for the cron-log UI
            started_at=started,
            finished_at=timezone.now(),
            note=f"sent={sent} failed={failed}",
        )
    logger.info("send_queued_notifications: processed=%s sent=%s failed=%s", len(ids), sent, failed)
    return {"processed": len(ids), "sent": sent, "failed": failed}


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
        if log.status in (NotificationStatus.SENT, NotificationStatus.SENDING):
            # Already delivered, or a drain run is sending it right now — never
            # reset a SENDING row (that would cause a double send).
            return
        # Reset a failed/pending row so the next drain retries it.
        log.status = NotificationStatus.PENDING
        log.subject, log.body, log.related_id = subject, body, related_id
        log.save(update_fields=["status", "subject", "body", "related_id"])
    # No broker: the row sits PENDING until the next send_queued_notifications run.


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
# Scheduled: expiry reminders (run by the send_expiry_reminders command)
# ---------------------------------------------------------------------------
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
