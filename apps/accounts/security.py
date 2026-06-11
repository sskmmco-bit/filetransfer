"""Login throttle + hard block (§5.9).

A progressive throttle keyed on both the typed identifier and the client IP
guards the login endpoint *before* any credential or 2FA processing. Recent
failed LoginAttempt rows drive the decision; a successful attempt clears the
counters for that identifier/IP pair. The block is deliberately generic to the
caller so the UI never reveals which accounts exist.
"""
from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from apps.audit.models import ActivityAction, ActivityLog, LoginAttempt

# Tuning — conservative defaults; can move to SiteSettings later.
WINDOW = timedelta(minutes=15)      # how far back failures count
MAX_FAILURES_PER_IDENTIFIER = 5     # block the identifier after this many
MAX_FAILURES_PER_IP = 20            # block a noisy IP after this many


def _recent_failures(**filters) -> int:
    since = timezone.now() - WINDOW
    return LoginAttempt.objects.filter(
        successful=False, created_at__gte=since, **filters
    ).count()


def is_blocked(identifier: str, ip: str | None) -> bool:
    """True if this identifier or IP has too many recent failures."""
    identifier = (identifier or "").strip()
    if identifier and _recent_failures(identifier__iexact=identifier) >= MAX_FAILURES_PER_IDENTIFIER:
        return True
    if ip and _recent_failures(ip_address=ip) >= MAX_FAILURES_PER_IP:
        return True
    return False


def record_attempt(identifier: str, ip: str | None, *, success: bool, user_agent: str = "") -> None:
    """Persist a LoginAttempt row (feeds the throttle and the audit trail)."""
    LoginAttempt.objects.create(
        identifier=(identifier or "").strip()[:254],
        ip_address=ip,
        successful=success,
        user_agent=(user_agent or "")[:255],
    )


def log_activity(actor, action: str, message: str, ip: str | None, user_agent: str = "") -> None:
    ActivityLog.objects.create(
        actor=actor if (actor and getattr(actor, "pk", None)) else None,
        action=action,
        message=message[:512],
        ip_address=ip,
        user_agent=(user_agent or "")[:255],
    )


# Re-export for convenient imports in views.
__all__ = ["is_blocked", "record_attempt", "log_activity", "ActivityAction"]
