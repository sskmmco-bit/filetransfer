"""Files background jobs (§5.6.2, §5.8, §5.11).

Formerly Celery tasks; now plain functions:

generate_thumbnail   — thumbnail one image. Best-effort: failures are logged and
                       bump thumbnail_attempts (so a bad image isn't retried
                       forever). Sources larger than THUMBNAIL_MAX_SOURCE_BYTES are
                       skipped.
generate_pending_thumbnails — async batch run by the generate_pending_thumbnails
                       management command (systemd timer ~2 min): finds active
                       images with no thumbnail yet and generates them OFF the
                       request thread (activation no longer blocks on this).
purge_expired_files  — daily cleanup, run by the purge_expired_files management
                       command: retention + per-file expiry soft-deletes,
                       abandoned-upload hard-delete + temp cleanup, and a CronLog.
"""
from __future__ import annotations

import io
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)

THUMBNAIL_SIZE = (320, 320)
# Skip thumbnailing originals larger than this (decode cost / memory on a small box).
THUMBNAIL_MAX_SOURCE_BYTES = 25 * 1024 * 1024  # 25 MiB
# Stop retrying an image after this many failed generation attempts.
THUMBNAIL_MAX_ATTEMPTS = 3
# Max images one generate_pending_thumbnails tick processes (bounds a single run).
THUMBNAIL_BATCH_LIMIT = 25


def generate_thumbnail(stored_file_id: int) -> dict:
    """Create a JPEG thumbnail for image files and store it in MinIO."""
    from . import storage
    from .models import FileStatus, StoredFile

    sf = StoredFile.objects.filter(pk=stored_file_id, status=FileStatus.ACTIVE).first()
    if not sf or not sf.content_type.startswith("image/"):
        return {"thumbnail": False, "reason": "not an active image"}
    if sf.size and sf.size > THUMBNAIL_MAX_SOURCE_BYTES:
        logger.info("generate_thumbnail: skipping %s (%s bytes > cap)", stored_file_id, sf.size)
        return {"thumbnail": False, "reason": "source too large"}

    try:
        from PIL import Image

        body = storage.get_object_body(sf.storage_key)
        try:
            img = Image.open(io.BytesIO(body.read()))
        finally:
            body.close()
        img.thumbnail(THUMBNAIL_SIZE)
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=82)
        buf.seek(0)

        thumb_key = f"thumbnails/{sf.uuid}.jpg"
        storage.put_object(thumb_key, buf, content_type="image/jpeg")
        sf.thumbnail_key = thumb_key
        sf.save(update_fields=["thumbnail_key", "updated_at"])
        return {"thumbnail": True, "key": thumb_key}
    except Exception as exc:  # noqa: BLE001 — thumbnailing is best-effort
        # Count the failed attempt so a permanently-bad image stops being retried.
        sf.thumbnail_attempts = (sf.thumbnail_attempts or 0) + 1
        sf.save(update_fields=["thumbnail_attempts", "updated_at"])
        logger.warning("generate_thumbnail failed for %s (attempt %s): %s",
                       stored_file_id, sf.thumbnail_attempts, exc)
        return {"thumbnail": False, "reason": str(exc)}


def generate_pending_thumbnails(limit: int = THUMBNAIL_BATCH_LIMIT) -> dict:
    """Generate thumbnails for active images that don't have one yet (§5.8).

    Runs off the request thread on a systemd timer. Picks active image files with
    an empty thumbnail_key, within the size cap, and under the attempt cap, then
    thumbnails each. Writes a CronLog only when it actually processed something.
    """
    from apps.audit.models import CronLog
    from .models import FileStatus, StoredFile

    started = timezone.now()
    pending = (
        StoredFile.objects.filter(
            status=FileStatus.ACTIVE,
            content_type__startswith="image/",
            thumbnail_key="",
            thumbnail_attempts__lt=THUMBNAIL_MAX_ATTEMPTS,
        )
        .exclude(size__gt=THUMBNAIL_MAX_SOURCE_BYTES)
        .order_by("uploaded_at")
    )
    ids = list(pending.values_list("pk", flat=True)[:limit])

    generated = 0
    for pk in ids:
        if generate_thumbnail(pk).get("thumbnail"):
            generated += 1

    if ids:
        CronLog.objects.create(
            task_name="generate_pending_thumbnails",
            processed_count=len(ids),
            deleted_count=0,
            started_at=started,
            finished_at=timezone.now(),
            note=f"generated={generated}",
        )
    logger.info("generate_pending_thumbnails: processed=%s generated=%s", len(ids), generated)
    return {"processed": len(ids), "generated": generated}


def purge_expired_files() -> dict:
    """Daily housekeeping (§5.6.2 / §8). Four stable steps:

      1. Retention (gated by retention_enabled, default OFF): active files older
         than retention_days -> soft-delete.
      2. Expiry: active files whose expiry_date has passed -> soft-delete.
      3. Abandoned uploads: uploading/pending_metadata rows past session expiry
         -> hard-delete + remove temp objects.
      4. Write a CronLog row.
    """
    from apps.audit.models import CronLog
    from apps.config.models import SiteSettings

    from . import services, storage
    from .models import FileStatus, StoredFile, UploadSession

    started = timezone.now()
    now = timezone.now()
    today = timezone.localdate()
    settings_obj = SiteSettings.get()
    processed = 0
    deleted = 0

    # Step 1 — retention (only if enabled).
    if settings_obj.retention_enabled and settings_obj.retention_days:
        cutoff = now - timezone.timedelta(days=settings_obj.retention_days)
        retention_qs = StoredFile.objects.filter(
            status=FileStatus.ACTIVE, uploaded_at__lt=cutoff
        )
        for sf in retention_qs.iterator():
            processed += 1
            services.soft_delete_stored_file(sf.pk, reason="retention policy")
            deleted += 1

    # Step 2 — per-file expiry (safe to always run; only affects files the user
    # explicitly gave an expiry_date).
    expiry_qs = StoredFile.objects.filter(
        status=FileStatus.ACTIVE, expiry_date__lt=today
    )
    for sf in expiry_qs.iterator():
        processed += 1
        services.soft_delete_stored_file(sf.pk, reason="per-file expiry")
        deleted += 1

    # Step 3 — abandoned uploads (hard-delete + temp cleanup).
    abandoned = StoredFile.objects.filter(
        status__in=[FileStatus.UPLOADING, FileStatus.PENDING_METADATA],
        upload_session__expires_at__lte=now,
    )
    for sf in abandoned.iterator():
        processed += 1
        session = UploadSession.objects.filter(stored_file=sf).first()
        if session and session.is_chunked and session.multipart_upload_id:
            storage.abort_multipart(sf.temp_key, session.multipart_upload_id)
        storage.delete_object(sf.temp_key)
        sf.delete()  # cascades the session
        deleted += 1

    # Step 4 — record the run.
    CronLog.objects.create(
        task_name="purge_expired_files",
        processed_count=processed,
        deleted_count=deleted,
        started_at=started,
        finished_at=timezone.now(),
        note=f"retention_enabled={settings_obj.retention_enabled}",
    )
    logger.info("purge_expired_files: processed=%s deleted=%s", processed, deleted)
    return {"processed_count": processed, "deleted_count": deleted}
