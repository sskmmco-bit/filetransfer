"""Daily cleanup (§5.6.2): retention + per-file expiry soft-deletes, abandoned
upload hard-delete + temp cleanup, and a CronLog row. Run on a daily systemd timer.

Replaces the Celery Beat schedule for this job. The logic lives in
apps.files.jobs.purge_expired_files.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.files.jobs import purge_expired_files


class Command(BaseCommand):
    help = "Purge expired/retained files and abandoned uploads; write a CronLog row."

    def handle(self, *args, **opts):
        result = purge_expired_files()
        self.stdout.write(
            f"processed={result['processed_count']} deleted={result['deleted_count']}"
        )
