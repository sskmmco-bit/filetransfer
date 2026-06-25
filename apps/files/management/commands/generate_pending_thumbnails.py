"""Async thumbnail generation (§5.8): generate thumbnails for active images that
don't have one yet, off the request thread. Run on a ~2-minute systemd timer.

Replaces the old inline (synchronous) thumbnailing on activation. The logic lives
in apps.files.jobs.generate_pending_thumbnails.
"""
from __future__ import annotations

from django.core.management.base import BaseCommand

from apps.files.jobs import THUMBNAIL_BATCH_LIMIT, generate_pending_thumbnails


class Command(BaseCommand):
    help = "Generate thumbnails for active images that have none yet."

    def add_arguments(self, parser):
        parser.add_argument(
            "--limit", type=int, default=THUMBNAIL_BATCH_LIMIT,
            help="Max images to process this run (default: %(default)s).",
        )

    def handle(self, *args, **opts):
        result = generate_pending_thumbnails(limit=opts["limit"])
        self.stdout.write(
            f"processed={result['processed']} generated={result['generated']}"
        )
