"""Backfill content_type for files stored as the generic octet-stream.

When libmagic is unavailable at upload time, magic-byte sniffing falls back to
application/octet-stream for every file, which suppresses previews and image
thumbnailing. This re-derives a usable content type from the filename extension
for existing rows. Safe and idempotent.

Usage:
    python manage.py fix_content_types --dry-run
    python manage.py fix_content_types
"""
from __future__ import annotations

import mimetypes

from django.core.management.base import BaseCommand
from django.db.models import Q

from apps.files.models import StoredFile


class Command(BaseCommand):
    help = "Re-derive content_type from the filename for octet-stream/empty rows."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true",
                            help="Show what would change without saving.")

    def handle(self, *args, **opts):
        qs = StoredFile.objects.filter(
            Q(content_type="") | Q(content_type="application/octet-stream")
        )
        scanned = 0
        updated = 0
        for sf in qs.iterator():
            scanned += 1
            guessed, _ = mimetypes.guess_type(sf.original_filename)
            if not guessed or guessed == sf.content_type:
                continue
            if opts["dry_run"]:
                self.stdout.write(f"  {sf.original_filename}: -> {guessed}")
                updated += 1
                continue
            sf.content_type = guessed
            sf.save(update_fields=["content_type", "updated_at"])
            updated += 1

        self.stdout.write(self.style.SUCCESS(
            f"Scanned {scanned} row(s); "
            + (f"{updated} would change (dry run)." if opts["dry_run"]
               else f"updated {updated}.")
        ))
        if not opts["dry_run"] and updated:
            self.stdout.write(
                "Image thumbnails for these will be generated on the next "
                "generate_pending_thumbnails run."
            )
