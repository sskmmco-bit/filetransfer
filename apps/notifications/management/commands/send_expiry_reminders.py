"""Queue expiry-reminder emails (§5.11). Run on a daily systemd timer.

Enqueues reminders for files expiring within the reminder window; actual SMTP
delivery happens on the next send_queued_notifications drain.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.notifications.jobs import send_expiry_reminders


class Command(BaseCommand):
    help = "Queue expiry-reminder emails for files expiring within the reminder window."

    def handle(self, *args, **opts):
        result = send_expiry_reminders()
        self.stdout.write(
            f"target_date={result['target_date']} queued={result['queued']}"
        )
