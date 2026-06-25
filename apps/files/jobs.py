"""Files background jobs (§5.6.2, §5.8, §5.11).

Formerly Celery tasks; now plain functions:

generate_thumbnail   — image thumbnailing, called synchronously on activation
                       (apps.files.services). Best-effort: failures are logged and
                       never block activation. Sources larger than
                       THUMBNAIL_MAX_SOURCE_BYTES are skipped so a huge image can't
                       stall the request thread.
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
# Skip thumbnailing originals larger than this — generation runs inline on the
# request thread (no Celery worker), so a huge image would stall the response.
THUMBNAIL_MAX_SOURCE_BYTES = 25 * 1024 * 1024  # 25 MiB


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
        logger.warning("generate_thumbnail failed for %s: %s", stored_file_id, exc)
        return {"thumbnail": False, "reason": str(exc)}


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
