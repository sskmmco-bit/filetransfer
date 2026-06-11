"""Import orphaned MinIO objects (§ Phase 5).

Scans the bucket under the `files/` prefix and creates a StoredFile record for
any object that no StoredFile currently references — e.g. files restored from a
backup or left behind by a previous system. Assigns them to a SuperAdmin owner.

Usage:
    python manage.py import_orphans --dry-run
    python manage.py import_orphans --owner alice
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.accounts.models import RoleSlug
from apps.files import storage
from apps.files.models import FileStatus, StoredFile

User = get_user_model()


class Command(BaseCommand):
    help = "Create StoredFile records for MinIO objects under files/ that are untracked."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="List orphans without importing.")
        parser.add_argument("--owner", help="Username to own imported files (default: a SuperAdmin).")

    def handle(self, *args, **opts):
        owner = self._resolve_owner(opts.get("owner"))
        known = set(
            StoredFile.objects.exclude(storage_key="").values_list("storage_key", flat=True)
        )

        client = storage.get_client()
        paginator = client.get_paginator("list_objects_v2")
        created = 0
        scanned = 0
        for page in paginator.paginate(Bucket=storage.bucket(), Prefix="files/"):
            for obj in page.get("Contents", []):
                scanned += 1
                key = obj["Key"]
                if key in known:
                    continue
                tail = key.rsplit("/", 1)[-1]
                name = tail.split("-", 1)[-1] if "-" in tail else tail
                if opts["dry_run"]:
                    self.stdout.write(f"  orphan: {key}")
                    continue
                StoredFile.objects.create(
                    owner=owner,
                    original_filename=name[:255],
                    storage_key=key,
                    size=obj.get("Size", 0),
                    status=FileStatus.ACTIVE,
                    uploaded_at=timezone.now(),
                    title=name,
                )
                created += 1

        self.stdout.write(self.style.SUCCESS(
            f"Scanned {scanned} object(s); "
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
