"""Import orphaned blobs from local disk (§ Phase 5).

Walks FILE_STORAGE_ROOT under the `files/` prefix and creates a StoredFile
record for any blob that no StoredFile currently references — e.g. files
restored from a backup or left behind by a previous system. Assigns them to a
SuperAdmin owner.

Usage:
    python manage.py import_orphans --dry-run
    python manage.py import_orphans --owner alice
"""
from __future__ import annotations

import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import RoleSlug
from apps.files.models import FileStatus, StoredFile

User = get_user_model()


class Command(BaseCommand):
    help = "Create StoredFile records for on-disk blobs under files/ that are untracked."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List orphans without importing.")
        parser.add_argument("--owner", help="Username to own imported files (default: a SuperAdmin).")

    def handle(self, *args, **opts):
        owner = self._resolve_owner(opts.get("owner"))
        known = set(
            StoredFile.objects.exclude(storage_key="").values_list("storage_key", flat=True)
        )

        root = os.path.realpath(str(settings.FILE_STORAGE_ROOT))
        files_dir = os.path.join(root, "files")
        created = 0
        scanned = 0
        for dirpath, _dirnames, filenames in os.walk(files_dir):
            for fname in filenames:
                scanned += 1
                abspath = os.path.join(dirpath, fname)
                # Key is the path relative to the storage root, POSIX-style.
                key = os.path.relpath(abspath, root).replace(os.sep, "/")
                if key in known:
                    continue
                name = fname.split("-", 1)[-1] if "-" in fname else fname
                if opts["dry_run"]:
                    self.stdout.write(f"  orphan: {key}")
                    continue
                StoredFile.objects.create(
                    owner=owner,
                    original_filename=name[:255],
                    storage_key=key,
                    size=os.path.getsize(abspath),
                    status=FileStatus.ACTIVE,
                    uploaded_at=timezone.now(),
                    title=name,
                )
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Scanned {scanned} blob(s); "
            + ("dry run — nothing imported." if opts["dry_run"] else f"imported {created} orphan(s).")
        ))

    def _resolve_owner(self, username):
        if username:
            owner = User.objects.filter(username=username).first()
            if not owner:
                raise CommandError(f"No user named {username!r}.")
            return owner
        owner = (
            User.objects.filter(role__slug=RoleSlug.SUPERADMIN).first()
            or User.objects.filter(is_superuser=True).first()
        )
        if not owner:
            raise CommandError("No SuperAdmin/superuser found to own imported files.")
        return owner
