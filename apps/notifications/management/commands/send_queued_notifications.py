"""Drain the deferred-email queue (§5.11). Run on a short systemd timer (~2 min).

Replaces the Celery worker for deferred mail. Claims pending NotificationLog rows
under a short row lock and sends them over SMTP outside the lock; overlap-safe via
select_for_update(skip_locked=True) (see apps.notifications.jobs.drain_pending).
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.notifications.jobs import DRAIN_BATCH, drain_pending


class Command(BaseCommand):
    help = "Send queued (deferred) notification emails from NotificationLog."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=DRAIN_BATCH,
            help=f"Max rows to process this run (default {DRAIN_BATCH}).",
        )

    def handle(self, *args, **opts):
        result = drain_pending(limit=opts["limit"])
        self.stdout.write(
            f"processed={result['processed']} sent={result['sent']} failed={result['failed']}"
        )
