"""Files domain model (§5.5, §5.8).

The lifecycle is a small, explicit state machine (§5.6.2):

    uploading -> pending_metadata -> active -> deleted

`complete transfer` (bytes written + validated) and `activate file` (metadata
saved, file goes live) are deliberately distinct steps — never both "finalize".
Blobs live in MinIO: a temp object during upload, copied to its final key
`files/{year}/{month}/{uuid}-{name}` on activation. Public-link and
encryption-at-rest columns arrive in Phase 3 / Phase 6.
"""
from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.db import models
from django.utils import timezone
from django.utils.text import slugify


class Category(models.Model):
    name = models.CharField(max_length=80, unique=True)
    slug = models.SlugField(max_length=90, unique=True, blank=True)
    description = models.CharField(max_length=255, blank=True)

    class Meta:
        db_table = "files_category"
        ordering = ("name",)
        verbose_name_plural = "categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)[:90]
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class FileStatus(models.TextChoices):
    UPLOADING = "uploading", "Uploading"
    PENDING_METADATA = "pending_metadata", "Pending metadata"
    ACTIVE = "active", "Active"
    # TRASHED keeps the row AND the stored object — recoverable from the admin
    # Trash. DELETED is terminal: the object is purged, only the audit row remains.
    TRASHED = "trashed", "In trash"
    DELETED = "deleted", "Deleted"


class StoredFile(models.Model):
    """A single stored file and its metadata; one MinIO object backs it."""

    uuid = models.UUIDField(default=uuid.uuid4, editable=False, unique=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="uploaded_files"
    )

    # Transfer facts (set during Step 1).
    original_filename = models.CharField(max_length=255)
    size = models.BigIntegerField(default=0)
    content_type = models.CharField(max_length=128, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)

    # Object keys: a temp key while uploading, a final key once active.
    temp_key = models.CharField(max_length=512, blank=True)
    storage_key = models.CharField(max_length=512, blank=True)
    thumbnail_key = models.CharField(max_length=512, blank=True)
    # Number of thumbnail-generation attempts. The async generate_pending_thumbnails
    # job stops retrying an image once this exceeds its cap (a permanently-bad image
    # shouldn't be reprocessed on every tick). 0 = not yet attempted.
    thumbnail_attempts = models.PositiveSmallIntegerField(default=0)

    # Metadata (collected in Step 2).
    title = models.CharField(max_length=255, blank=True)
    description = models.TextField(blank=True)
    # Optional personal note shown to recipients (in the assignment email).
    message = models.TextField(blank=True)
    categories = models.ManyToManyField(Category, blank=True, related_name="files")
    # Groups this file is placed into (a shared space). Membership is dynamic:
    # anyone currently in the group sees the file, even if they joined later.
    # Distinct from per-recipient FileAssignment (which drives email + inbox).
    groups = models.ManyToManyField(
        "accounts.Group", blank=True, related_name="files"
    )
    is_hidden = models.BooleanField(default=False)
    # Per-user favourites (Dropbox-style "starred"). Through StarredFile so we can
    # order by when it was starred; membership is per viewer, not global.
    starred_by = models.ManyToManyField(
        settings.AUTH_USER_MODEL, through="StarredFile",
        related_name="starred_files", blank=True,
    )
    # Dormant: the custom-fields feature was removed from the UI. Column kept to
    # avoid a destructive migration; no code reads or writes it anymore.
    custom_fields = models.JSONField(default=dict, blank=True)

    # Public sharing (link minting is Phase 3; the columns live here now).
    is_public = models.BooleanField(default=False)
    public_token = models.CharField(max_length=64, blank=True, db_index=True)
    public_require_email_verify = models.BooleanField(default=False)
    # Dropbox-style link controls. For a multi-file "bundle" these are stamped
    # identically on every file sharing the same public_token.
    public_password_hash = models.CharField(max_length=255, blank=True)  # blank = no password
    public_allow_download = models.BooleanField(default=True)  # False = preview-only

    # Limits / expiry.
    expiry_date = models.DateField(null=True, blank=True)
    download_limit = models.PositiveIntegerField(null=True, blank=True)
    download_count = models.PositiveIntegerField(default=0)

    status = models.CharField(
        max_length=20, choices=FileStatus.choices, default=FileStatus.UPLOADING, db_index=True
    )

    created_at = models.DateTimeField(default=timezone.now)
    uploaded_at = models.DateTimeField(null=True, blank=True)  # set on activation
    updated_at = models.DateTimeField(auto_now=True)
    deleted_at = models.DateTimeField(null=True, blank=True)  # set when trashed
    # Who sent it to trash (null = system, e.g. retention/expiry cleanup).
    deleted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="trashed_files",
    )

    class Meta:
        db_table = "files_stored_file"
        ordering = ("-created_at",)
        indexes = [
            models.Index(fields=["owner", "status"]),
            models.Index(fields=["status", "created_at"]),
            # Trigram GIN indexes so the file-search ILIKE '%term%' on the two
            # identifying columns is index-assisted (not a sequential scan).
            # Requires the pg_trgm extension (created in migration 0011).
            GinIndex(
                name="file_fname_trgm",
                fields=["original_filename"],
                opclasses=["gin_trgm_ops"],
            ),
            GinIndex(
                name="file_title_trgm",
                fields=["title"],
                opclasses=["gin_trgm_ops"],
            ),
            # Partial indexes for the daily purge job's predicates (retention +
            # per-file expiry). Partial on status=ACTIVE keeps them tiny — they
            # only cover live files, which is all the purge scans.
            models.Index(
                name="file_active_uploaded_at",
                fields=["uploaded_at"],
                condition=models.Q(status=FileStatus.ACTIVE),
            ),
            models.Index(
                name="file_active_expiry_date",
                fields=["expiry_date"],
                condition=models.Q(status=FileStatus.ACTIVE),
            ),
        ]

    def __str__(self):
        return self.title or self.original_filename

    @property
    def is_active(self) -> bool:
        return self.status == FileStatus.ACTIVE

    @property
    def is_trashed(self) -> bool:
        return self.status == FileStatus.TRASHED

    @property
    def display_name(self) -> str:
        return self.title or self.original_filename

    def build_storage_key(self) -> str:
        """Final key layout: files/{year}/{month}/{uuid}-{name} (§5.5)."""
        now = self.uploaded_at or timezone.now()
        return f"files/{now:%Y}/{now:%m}/{self.uuid}-{self.original_filename}"

    @property
    def is_expired(self) -> bool:
        return bool(self.expiry_date and self.expiry_date < timezone.localdate())

    def download_limit_reached(self) -> bool:
        return bool(self.download_limit is not None and self.download_count >= self.download_limit)

    @property
    def has_public_password(self) -> bool:
        return bool(self.public_password_hash)

    @property
    def public_access_label(self) -> str:
        """Derived access mode for the link (no separate column)."""
        if not self.is_public:
            return "restricted"
        return "tracked" if self.public_require_email_verify else "public"


class UploadSession(models.Model):
    """Tracks an in-progress transfer; maps a chunked upload to MinIO multipart.

    Exactly one session per StoredFile in flight. Sessions expire after ~24h;
    the purge task hard-deletes abandoned ones and removes their temp objects.
    """

    token = models.UUIDField(default=uuid.uuid4, editable=False, unique=True, db_index=True)
    stored_file = models.OneToOneField(
        StoredFile, on_delete=models.CASCADE, related_name="upload_session"
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="upload_sessions"
    )

    is_chunked = models.BooleanField(default=False)
    multipart_upload_id = models.CharField(max_length=255, blank=True)
    # Completed parts for the S3 multipart: [{"PartNumber": n, "ETag": "..."}].
    parts = models.JSONField(default=list, blank=True)

    declared_size = models.BigIntegerField(default=0)
    received_size = models.BigIntegerField(default=0)

    created_at = models.DateTimeField(default=timezone.now)
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        db_table = "files_upload_session"

    def save(self, *args, **kwargs):
        if not self.expires_at:
            self.expires_at = (self.created_at or timezone.now()) + timezone.timedelta(hours=24)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"session {self.token} ({self.stored_file_id})"

    @property
    def is_expired(self) -> bool:
        return timezone.now() >= self.expires_at

    def next_part_number(self) -> int:
        return len(self.parts) + 1


class ShareLink(models.Model):
    """A public share link — a permission wrapper around one or more files.

    A link is NOT a file: the same file can be covered by several links over
    time (an HR link, a client link, …), each with its own controls and stats.
    Settings (password, email-verify, preview-only, expiry, download limit) and
    the running view/download counters live here, not on the file. The public
    download flow (`apps/public`) resolves a link by its `token`.
    """

    token = models.CharField(max_length=64, unique=True, db_index=True)
    name = models.CharField(max_length=120, blank=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="share_links"
    )
    files = models.ManyToManyField(
        StoredFile, through="ShareLinkFile", related_name="share_links"
    )

    # Link-level controls.
    expires_at = models.DateField(null=True, blank=True)
    password_hash = models.CharField(max_length=255, blank=True)  # blank = no password
    require_email_verify = models.BooleanField(default=False)
    # Whitelist of lowercased emails allowed to verify. Empty = any verified
    # email (when require_email_verify). Non-empty implies require_email_verify.
    allowed_emails = models.JSONField(default=list, blank=True)
    allow_download = models.BooleanField(default=True)  # False = preview-only
    download_limit = models.PositiveIntegerField(null=True, blank=True)  # total across files

    # Stats / state.
    download_count = models.PositiveIntegerField(default=0)
    view_count = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(default=timezone.now)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "files_share_link"
        ordering = ("-created_at",)
        indexes = [models.Index(fields=["created_by", "is_active"])]

    def __str__(self):
        return self.name or f"link {self.token[:8]}"

    @staticmethod
    def default_name(n: int) -> str:
        return f"Link to {n} file{'s' if n != 1 else ''}"

    @property
    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at < timezone.localdate())

    @property
    def is_live(self) -> bool:
        return self.is_active and not self.is_expired

    @property
    def has_password(self) -> bool:
        return bool(self.password_hash)

    @property
    def access_label(self) -> str:
        if self.allowed_emails:
            return "restricted"
        return "tracked" if self.require_email_verify else "public"

    def is_email_allowed(self, email: str) -> bool:
        """True if this email may verify (any email when no whitelist is set)."""
        if not self.allowed_emails:
            return True
        return (email or "").strip().lower() in self.allowed_emails

    def download_limit_reached(self) -> bool:
        return bool(self.download_limit is not None and self.download_count >= self.download_limit)

    @property
    def downloads_remaining(self) -> int | None:
        if self.download_limit is None:
            return None
        return max(0, self.download_limit - self.download_count)

    @property
    def days_remaining(self) -> int | None:
        if not self.expires_at:
            return None
        return (self.expires_at - timezone.localdate()).days

    def build_url(self, request=None) -> str:
        """Canonical short URL. We store only the token and build the URL on
        demand from PUBLIC_BASE_URL (or the request host) — domain-safe."""
        from django.conf import settings as dj_settings

        base = (getattr(dj_settings, "PUBLIC_BASE_URL", "") or "").rstrip("/")
        if not base and request is not None:
            base = f"{request.scheme}://{request.get_host()}"
        return f"{base}/s/{self.token}"


class ShareLinkFile(models.Model):
    """Membership row: one file inside one share link."""

    share_link = models.ForeignKey(
        ShareLink, on_delete=models.CASCADE, related_name="link_files"
    )
    stored_file = models.ForeignKey(
        StoredFile, on_delete=models.CASCADE, related_name="link_memberships"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "files_share_link_file"
        unique_together = (("share_link", "stored_file"),)
        ordering = ("created_at",)

    def __str__(self):
        return f"{self.share_link_id} ⊇ {self.stored_file_id}"


class StarredFile(models.Model):
    """A user's favourite mark on a file (per-viewer, not global)."""

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="file_stars"
    )
    stored_file = models.ForeignKey(
        StoredFile, on_delete=models.CASCADE, related_name="stars"
    )
    created_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "files_starred_file"
        unique_together = (("user", "stored_file"),)
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.user_id} ★ {self.stored_file_id}"


class FileAssignment(models.Model):
    """One row per recipient a file is assigned to (§5.5/§5.8)."""

    stored_file = models.ForeignKey(
        StoredFile, on_delete=models.CASCADE, related_name="assignments"
    )
    recipient = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="file_assignments"
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assignments_made",
    )
    created_at = models.DateTimeField(default=timezone.now)
    notified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "files_file_assignment"
        unique_together = (("stored_file", "recipient"),)
        ordering = ("-created_at",)

    def __str__(self):
        return f"{self.stored_file_id} -> {self.recipient_id}"
