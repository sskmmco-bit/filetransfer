"""Notification bookkeeping (§5.11).

NotificationLog enforces at-most-once delivery for deferred (Celery) email: a
unique idempotency_key means the same logical notification is enqueued once.
`sent_at` (the plan's notification_sent_at) is set ONLY when SMTP delivery
actually succeeds. On permanent failure the row is marked failed so it can be
re-enqueued later.
"""
from __future__ import annotations

from django.db import models
from django.utils import timezone


class EmailType(models.TextChoices):
    ASSIGNMENT = "assignment", "File assignment"
    WELCOME = "welcome", "Welcome"
    EXPIRY = "expiry", "Expiry reminder"


class NotificationStatus(models.TextChoices):
    PENDING = "pending", "Pending"
    SENT = "sent", "Sent"
    FAILED = "failed", "Failed"


class NotificationLog(models.Model):
    idempotency_key = models.CharField(max_length=128, unique=True)
    email_type = models.CharField(max_length=20, choices=EmailType.choices)
    recipient = models.EmailField()
    subject = models.CharField(max_length=255)
    body = models.TextField()
    # Optional link back to a domain row to stamp on success (e.g. FileAssignment).
    related_id = models.BigIntegerField(null=True, blank=True)

    status = models.CharField(
        max_length=10, choices=NotificationStatus.choices, default=NotificationStatus.PENDING
    )
    attempts = models.PositiveSmallIntegerField(default=0)
    error = models.TextField(blank=True)
    created_at = models.DateTimeField(default=timezone.now)
    sent_at = models.DateTimeField(null=True, blank=True)  # notification_sent_at

    class Meta:
        db_table = "notifications_log"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["status", "email_type"])]

    def __str__(self):
        return f"{self.email_type} -> {self.recipient} [{self.status}]"
