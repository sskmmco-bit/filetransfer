"""Audit & security models (§5.9, §5.14, §5.15).

Phase 1 ships three tables:
  - ActivityLog  : durable record of meaningful user/system actions.
  - LoginAttempt : every authentication attempt; feeds the login throttle.
  - CronLog      : per-run summary of scheduled Celery tasks (e.g. purge).

DownloadEvent (§5.10) is added in Phase 3 alongside the download flow.
All records capture the real client IP via apps.core.utils.get_client_ip().
"""
from __future__ import annotations

from django.conf import settings
from django.db import models
from django.utils import timezone


class ActivityAction(models.TextChoices):
    LOGIN = "login", "Login"
    LOGOUT = "logout", "Logout"
    LOGIN_FAILED = "login_failed", "Login failed"
    USER_CREATED = "user_created", "User created (JIT/admin)"
    UPLOAD = "upload", "File uploaded"
    DOWNLOAD = "download", "File downloaded"
    DELETE = "delete", "File deleted"
    SETTINGS_CHANGED = "settings_changed", "Settings changed"
    OTHER = "other", "Other"


class ActivityLog(models.Model):
    """Durable, human-readable trail of meaningful actions."""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="activity_logs",
    )
    action = models.CharField(max_length=32, choices=ActivityAction.choices)
    message = models.CharField(max_length=512, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_activity_log"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["actor", "created_at"])]

    def __str__(self):
        who = self.actor or "anonymous"
        return f"{who} {self.action} @ {self.created_at:%Y-%m-%d %H:%M}"


class LoginAttempt(models.Model):
    """One row per authentication attempt; the throttle reads recent failures.

    The raw typed identifier is stored (not a user FK) because a failed attempt
    may not resolve to any account, and the throttle keys on identifier + IP.
    """

    identifier = models.CharField(max_length=254, db_index=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True, db_index=True)
    successful = models.BooleanField(default=False)
    user_agent = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_login_attempt"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["identifier", "created_at"]),
            models.Index(fields=["ip_address", "created_at"]),
        ]

    def __str__(self):
        state = "ok" if self.successful else "fail"
        return f"{self.identifier} [{state}] @ {self.created_at:%Y-%m-%d %H:%M}"


class DownloadEvent(models.Model):
    """One row per download slot reserved (§5.10).

    `user` is null for anonymous public downloads; `visitor_email` records the
    verified email in that case. Created after a slot is successfully reserved.
    """

    stored_file = models.ForeignKey(
        "files.StoredFile", on_delete=models.CASCADE, related_name="download_events"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="download_events",
    )
    visitor_email = models.EmailField(blank=True)
    via_public_link = models.BooleanField(default=False)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_download_event"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["stored_file", "created_at"])]

    def __str__(self):
        who = self.user or self.visitor_email or "anonymous"
        return f"{who} downloaded {self.stored_file_id} @ {self.created_at:%Y-%m-%d %H:%M}"


class CronLog(models.Model):
    """Summary row written by scheduled tasks (e.g. purge_expired_files §5.6.2)."""

    task_name = models.CharField(max_length=128, db_index=True)
    processed_count = models.PositiveIntegerField(default=0)
    deleted_count = models.PositiveIntegerField(default=0)
    note = models.TextField(blank=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        db_table = "audit_cron_log"
        ordering = ("-finished_at",)

    def __str__(self):
        return f"{self.task_name}: processed={self.processed_count} deleted={self.deleted_count}"
