"""Files domain services (§5.5, §5.6.1, §5.8).

Two clearly separated steps:

  complete_transfer()  — bytes are written and validated (full-file SHA-256 +
                         magic-byte content sniff + policy checks). Moves the
                         file to `pending_metadata`. Never touches the final key.
  activate_file()      — metadata is saved and the file goes live: copy temp ->
                         final key, set uploaded_at, create FileAssignment rows,
                         generate the thumbnail + queue notification emails, delete
                         the temp object/session. Row-locked and idempotent: a
                         retried request never double-copies objects or double-sends
                         mail.
"""
from __future__ import annotations

import hashlib
import secrets

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils import timezone

from . import storage
from .models import FileAssignment, FileStatus, StoredFile, UploadSession

# Transport threshold: <= 50 MiB goes direct, larger uses the chunked path.
CHUNK_THRESHOLD = 50 * 1024 * 1024
# Hard upper bound for a single file.
MAX_UPLOAD_SIZE = 5 * 1024 * 1024 * 1024  # 5 GiB
# S3 multipart parts must be >= 5 MiB (except the last).
MIN_PART_SIZE = 5 * 1024 * 1024
READ_CHUNK = 8 * 1024 * 1024

# Extensions refused outright. This is a general-purpose internal file-transfer
# system, so by default ALL file types are accepted (executables included).
# Add extensions here only if a deployment needs to block specific types.
DISALLOWED_EXTENSIONS: set[str] = set()


def sniff_content_type(sample: bytes) -> str:
    """Magic-byte content type from the first bytes of the object (§5.6.1b)."""
    try:
        import magic  # python-magic (libmagic)

        return magic.from_buffer(sample, mime=True) or "application/octet-stream"
    except Exception:  # noqa: BLE001 — never fail the upload on a sniff error
        return "application/octet-stream"


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _stream_digest_and_type(key: str) -> tuple[str, str, int]:
    """Single pass over the object: returns (sha256_hex, content_type, size)."""
    body = storage.get_object_body(key)
    hasher = hashlib.sha256()
    size = 0
    content_type = None
    try:
        while True:
            chunk = body.read(READ_CHUNK)
            if not chunk:
                break
            if content_type is None:
                content_type = sniff_content_type(chunk[:2048])
            hasher.update(chunk)
            size += len(chunk)
    finally:
        body.close()
    return hasher.hexdigest(), (content_type or "application/octet-stream"), size


@transaction.atomic
def complete_transfer(session: UploadSession) -> StoredFile:
    """Assemble + validate the uploaded bytes; move to pending_metadata (§5.5).

    Raises ValidationError on a policy violation, after removing the temp object
    and the failed StoredFile/session (the abandoned bytes never linger).
    """
    sf = StoredFile.objects.select_for_update().get(pk=session.stored_file_id)

    # Chunked uploads finish the S3 multipart now (parts already uploaded).
    if session.is_chunked and session.multipart_upload_id:
        storage.complete_multipart(sf.temp_key, session.multipart_upload_id, session.parts)

    sha256, content_type, size = _stream_digest_and_type(sf.temp_key)

    reason = _validate(sf.original_filename, content_type, size)
    if reason:
        storage.delete_object(sf.temp_key)
        session.delete()
        sf.delete()
        raise ValidationError(reason)

    sf.sha256 = sha256
    sf.content_type = content_type
    sf.size = size
    sf.status = FileStatus.PENDING_METADATA
    sf.save(update_fields=["sha256", "content_type", "size", "status", "updated_at"])
    return sf


def _validate(filename: str, content_type: str, size: int) -> str | None:
    """Return an error message if the upload is rejected, else None."""
    if size <= 0:
        return "The uploaded file is empty."
    if size > MAX_UPLOAD_SIZE:
        return "The file exceeds the maximum allowed size."
    if _extension(filename) in DISALLOWED_EXTENSIONS:
        return "This file type is not allowed."
    return None


@transaction.atomic
def activate_file(stored_file_id: int, *, recipient_ids=None, assigned_by=None,
                  notify: bool = True) -> StoredFile:
    """Copy temp->final, create assignments, enqueue tasks. Idempotent (§5.8).

    notify=False suppresses the deferred assignment-notification emails.
    """
    sf = StoredFile.objects.select_for_update().get(pk=stored_file_id)

    # Idempotent: a retried activation is a no-op once the file is live.
    if sf.status == FileStatus.ACTIVE:
        return sf

    # The uploaded bytes may have been removed (e.g. a cancelled/abandoned upload
    # was cleaned up) — fail clearly instead of letting CopyObject raise NoSuchKey.
    if not sf.temp_key or not storage.object_exists(sf.temp_key):
        raise ValidationError(
            "The uploaded file data is no longer available. "
            "Please delete this draft and upload the file again."
        )
    sf.uploaded_at = timezone.now()
    sf.storage_key = sf.build_storage_key()
    storage.copy_object(sf.temp_key, sf.storage_key)
    sf.status = FileStatus.ACTIVE
    sf.save(update_fields=["uploaded_at", "storage_key", "status", "updated_at"])

    # One FileAssignment per recipient.
    for rid in set(recipient_ids or []):
        FileAssignment.objects.get_or_create(
            stored_file=sf, recipient_id=rid, defaults={"assigned_by": assigned_by}
        )

    # Clean up the temp object + session, then enqueue async work.
    temp_key = sf.temp_key
    UploadSession.objects.filter(stored_file=sf).delete()
    if temp_key and temp_key != sf.storage_key:
        storage.delete_object(temp_key)
    sf.temp_key = ""
    sf.save(update_fields=["temp_key"])

    transaction.on_commit(lambda: _enqueue_post_activation(sf.pk, notify=notify))
    return sf


@transaction.atomic
def add_recipients(stored_file_id: int, *, recipient_ids=None, groups=None,
                   assigned_by=None, notify: bool = True) -> int:
    """Share an already-active file with more people/groups (§5.8).

    Creates a FileAssignment per new recipient and places the file into each
    group's shared space. Returns the number of *new* recipient assignments.
    Notification emails go only to the newly added recipients.
    """
    sf = StoredFile.objects.select_for_update().get(pk=stored_file_id)
    new_ids = []
    for rid in set(recipient_ids or []):
        _, created = FileAssignment.objects.get_or_create(
            stored_file=sf, recipient_id=rid, defaults={"assigned_by": assigned_by}
        )
        if created:
            new_ids.append(rid)
    if groups:
        sf.groups.add(*groups)
    if new_ids:
        from apps.audit.models import ActivityAction, ActivityLog

        n = len(new_ids)
        ActivityLog.objects.create(
            actor=assigned_by, action=ActivityAction.OTHER,
            message=f"Shared '{sf.display_name}' with {n} {'person' if n == 1 else 'people'}",
        )
    if notify and new_ids:
        ids = list(new_ids)
        transaction.on_commit(lambda: _notify_recipients(sf.pk, ids))
    return len(new_ids)


def _notify_recipients(stored_file_id: int, recipient_ids) -> None:
    """Send assignment-notification emails to specific recipients of a file."""
    from apps.notifications.jobs import enqueue_assignment_email

    for assignment in FileAssignment.objects.filter(
        stored_file_id=stored_file_id, recipient_id__in=list(recipient_ids)
    ).select_related("recipient", "assigned_by", "stored_file"):
        enqueue_assignment_email(assignment)


def _enqueue_post_activation(stored_file_id: int, *, notify: bool = True) -> None:
    """Post-activation work, run after the activation commits (§5.8, §5.11).

    Thumbnailing is NOT done here anymore — it runs asynchronously off the request
    thread via the generate_pending_thumbnails job (systemd timer), which picks up
    any active image with no thumbnail yet. Assignment emails are queued to
    NotificationLog for the send_queued_notifications drain.
    """
    from apps.notifications.jobs import enqueue_assignment_email

    from .models import FileAssignment

    if not notify:
        return
    # Deferred assignment-notification emails — one per recipient (at-most-once).
    for assignment in FileAssignment.objects.filter(
        stored_file_id=stored_file_id
    ).select_related("recipient", "assigned_by", "stored_file"):
        enqueue_assignment_email(assignment)


@transaction.atomic
def reserve_download_slot(stored_file_id: int) -> StoredFile | None:
    """Atomically reserve one download slot under a row lock (§5.10).

    Returns the refreshed StoredFile if a slot was reserved, or None if the file
    is unavailable (not active, expired, or the download limit is reached). The
    check-and-increment is a single locked transaction so concurrent visitors
    can never over-draw the last slot.
    """
    sf = StoredFile.objects.select_for_update().get(pk=stored_file_id)
    if sf.status != FileStatus.ACTIVE or sf.is_expired:
        return None
    if sf.download_limit is not None and sf.download_count >= sf.download_limit:
        return None
    sf.download_count += 1
    sf.save(update_fields=["download_count", "updated_at"])
    return sf


@transaction.atomic
def soft_delete_stored_file(stored_file_id: int, *, reason: str = "", actor=None) -> StoredFile:
    """Move an active file to Trash (§5.6.2): the row AND the stored object are
    KEPT so the file can be restored; only public sharing is withdrawn. Permanent
    removal of the object happens later via ``purge_stored_file``. Idempotent."""
    from apps.audit.models import ActivityAction, ActivityLog

    sf = StoredFile.objects.select_for_update().get(pk=stored_file_id)
    if sf.status in (FileStatus.TRASHED, FileStatus.DELETED):
        return sf

    # Drafts / in-flight uploads (PENDING_METADATA, UPLOADING) were never shared
    # and have no final stored object — restoring one to ACTIVE would be broken,
    # so discard them outright instead of parking them in the recoverable trash.
    if sf.status != FileStatus.ACTIVE:
        for key in (sf.temp_key, sf.storage_key, sf.thumbnail_key):
            if key:
                storage.delete_object(key)
        sf.status = FileStatus.DELETED
        sf.deleted_at = timezone.now()
        sf.is_public = False
        sf.public_token = ""
        sf.save(update_fields=["status", "deleted_at", "is_public",
                               "public_token", "updated_at"])
        ActivityLog.objects.create(
            actor=actor if (actor and getattr(actor, "pk", None)) else None,
            action=ActivityAction.DELETE,
            message=f"Discarded draft '{sf.display_name}'" + (f": {reason}" if reason else ""),
        )
        return sf

    # Withdraw public exposure while in trash — but keep the object so a restore
    # can put it back. Live share links stop resolving (they only serve ACTIVE).
    sf.status = FileStatus.TRASHED
    sf.deleted_at = timezone.now()
    sf.deleted_by = actor if (actor and getattr(actor, "pk", None)) else None
    sf.is_public = False
    sf.public_token = ""
    sf.save(update_fields=["status", "deleted_at", "deleted_by", "is_public",
                           "public_token", "updated_at"])

    ActivityLog.objects.create(
        actor=actor if (actor and getattr(actor, "pk", None)) else None,
        action=ActivityAction.DELETE,
        message=f"Moved '{sf.display_name}' to trash" + (f": {reason}" if reason else ""),
    )
    return sf


@transaction.atomic
def restore_stored_file(stored_file_id: int, *, actor=None) -> StoredFile:
    """Restore a trashed file back to ACTIVE. The object was never removed, so
    this just flips the status. No-op if the file isn't in trash."""
    from apps.audit.models import ActivityAction, ActivityLog

    sf = StoredFile.objects.select_for_update().get(pk=stored_file_id)
    if sf.status != FileStatus.TRASHED:
        return sf

    sf.status = FileStatus.ACTIVE
    sf.deleted_at = None
    sf.deleted_by = None
    sf.save(update_fields=["status", "deleted_at", "deleted_by", "updated_at"])

    # Re-establish the denormalised public hints in case the file is still a
    # member of a live share link (they were cleared when it was trashed).
    from . import sharelinks
    sharelinks.sync_file_public_flags([sf])

    ActivityLog.objects.create(
        actor=actor if (actor and getattr(actor, "pk", None)) else None,
        action=ActivityAction.DELETE,
        message=f"Restored '{sf.display_name}' from trash",
    )
    return sf


@transaction.atomic
def purge_stored_file(stored_file_id: int, *, reason: str = "", actor=None) -> StoredFile:
    """Permanently delete a file: remove the MinIO object(s) but keep the row +
    audit history (terminal DELETED state). Idempotent."""
    from apps.audit.models import ActivityAction, ActivityLog

    sf = StoredFile.objects.select_for_update().get(pk=stored_file_id)
    if sf.status == FileStatus.DELETED:
        return sf

    if sf.storage_key:
        storage.delete_object(sf.storage_key)
    if sf.thumbnail_key:
        storage.delete_object(sf.thumbnail_key)
    sf.status = FileStatus.DELETED
    sf.deleted_at = sf.deleted_at or timezone.now()
    sf.is_public = False
    sf.public_token = ""
    sf.save(update_fields=["status", "deleted_at", "is_public", "public_token", "updated_at"])

    ActivityLog.objects.create(
        actor=actor if (actor and getattr(actor, "pk", None)) else None,
        action=ActivityAction.DELETE,
        message=f"Permanently deleted '{sf.display_name}'" + (f": {reason}" if reason else ""),
    )
    return sf


# Public share links are now first-class entities — see apps/files/sharelinks.py.
