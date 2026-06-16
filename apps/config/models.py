"""SiteSettings singleton (§13.3).

Exactly one row, guarded by a unique singleton_key. Runtime-editable admin
settings live here; deployment constants live in settings.py. Phase 1 adds the
LDAP configuration (§5.7.3) — the bind password is Fernet-encrypted at rest
using settings.SECRETS_ENCRYPTION_KEY — and a session idle-timeout knob.
"""
from __future__ import annotations

from django.conf import settings as django_settings
from django.db import models


def _fernet():
    """Build a Fernet from SECRETS_ENCRYPTION_KEY, or raise a clear error."""
    from cryptography.fernet import Fernet  # local import: optional dependency

    key = getattr(django_settings, "SECRETS_ENCRYPTION_KEY", "") or ""
    if not key:
        raise RuntimeError(
            "SECRETS_ENCRYPTION_KEY is not set; cannot encrypt/decrypt secrets. "
            "Generate one with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


class CustomField(models.Model):
    """Admin-defined metadata field shown on the file form (§ Phase 5).

    Values are stored per-file in StoredFile.custom_fields (a JSON dict keyed by
    `key`), so adding a field needs no schema migration.
    """

    class FieldType(models.TextChoices):
        TEXT = "text", "Text"
        NUMBER = "number", "Number"
        DATE = "date", "Date"
        BOOLEAN = "boolean", "Yes/No"

    key = models.SlugField(max_length=50, unique=True)
    label = models.CharField(max_length=120)
    field_type = models.CharField(max_length=10, choices=FieldType.choices, default=FieldType.TEXT)
    required = models.BooleanField(default=False)
    active = models.BooleanField(default=True)
    order = models.PositiveSmallIntegerField(default=0)

    class Meta:
        db_table = "config_custom_field"
        ordering = ("order", "label")

    def __str__(self):
        return self.label


class SiteSettings(models.Model):
    singleton_key = models.CharField(max_length=8, unique=True, default="main", editable=False)

    site_name = models.CharField(max_length=120, default="MMFileTransfer")
    timezone = models.CharField(max_length=64, default="UTC")
    date_format = models.CharField(max_length=32, default="Y-m-d")
    pagination_size = models.PositiveSmallIntegerField(default=25)

    # Session (§5.9) — idle timeout in minutes; enforced by middleware/settings.
    session_idle_timeout_minutes = models.PositiveIntegerField(default=480)

    # Global retention (§5.6.2) — OFF by default; behaviour wired in Phase 5.
    retention_enabled = models.BooleanField(default=False)
    retention_days = models.PositiveSmallIntegerField(default=5)

    # Default per-user storage quota in GB (§quota). 0 => unlimited. Applied to
    # users whose own quota_bytes is NULL — i.e. every account that isn't given
    # an explicit quota (LDAP imports, JIT logins, plain console creates).
    default_user_quota_gb = models.PositiveIntegerField(default=2)

    # ----- LDAP (§5.7.3) — config ships in Phase 1; settings UI in Phase 6 -----
    ldap_enabled = models.BooleanField(default=False)
    ldap_server_uri = models.CharField(max_length=255, blank=True)  # e.g. ldap://dc.example.com:389
    ldap_bind_dn = models.CharField(max_length=255, blank=True)
    ldap_bind_password_encrypted = models.TextField(blank=True)
    ldap_base_dn = models.CharField(max_length=255, blank=True)
    ldap_user_search_filter = models.CharField(
        max_length=255, blank=True, default="(uid=%(user)s)"
    )
    ldap_use_ssl = models.BooleanField(default=False)
    ldap_connect_timeout = models.PositiveSmallIntegerField(default=5)

    # Attribute mapping (directory attr -> our field).
    ldap_attr_email = models.CharField(max_length=64, blank=True, default="mail")
    ldap_attr_employee_id = models.CharField(max_length=64, blank=True, default="employeeNumber")
    ldap_attr_first_name = models.CharField(max_length=64, blank=True, default="givenName")
    ldap_attr_last_name = models.CharField(max_length=64, blank=True, default="sn")

    # ----- Email / SMTP (§5.11, Phase 4) -----
    smtp_enabled = models.BooleanField(default=False)  # off => Django default backend (console in dev)
    smtp_host = models.CharField(max_length=255, blank=True)
    smtp_port = models.PositiveIntegerField(default=587)
    smtp_username = models.CharField(max_length=255, blank=True)
    smtp_password_encrypted = models.TextField(blank=True)
    smtp_use_tls = models.BooleanField(default=True)
    smtp_use_ssl = models.BooleanField(default=False)
    default_from_email = models.CharField(max_length=255, blank=True)
    notification_bcc = models.EmailField(blank=True)  # archive/audit BCC on deferred mail

    # Notification toggles / timing.
    send_welcome_email = models.BooleanField(default=True)
    send_assignment_email = models.BooleanField(default=True)
    expiry_reminder_days = models.PositiveSmallIntegerField(default=3)

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "config_site_settings"
        verbose_name = "Site settings"
        verbose_name_plural = "Site settings"

    def __str__(self):
        return self.site_name

    @classmethod
    def get(cls) -> "SiteSettings":
        obj, _ = cls.objects.get_or_create(singleton_key="main")
        return obj

    # ----- Encrypted bind password -----
    def set_ldap_bind_password(self, raw: str) -> None:
        """Encrypt and store the LDAP bind password (call .save() afterwards)."""
        if raw:
            self.ldap_bind_password_encrypted = _fernet().encrypt(raw.encode()).decode()
        else:
            self.ldap_bind_password_encrypted = ""

    def get_ldap_bind_password(self) -> str:
        """Decrypt the stored LDAP bind password (empty string if none set)."""
        if not self.ldap_bind_password_encrypted:
            return ""
        return _fernet().decrypt(self.ldap_bind_password_encrypted.encode()).decode()

    # ----- Encrypted SMTP password -----
    def set_smtp_password(self, raw: str) -> None:
        self.smtp_password_encrypted = _fernet().encrypt(raw.encode()).decode() if raw else ""

    def get_smtp_password(self) -> str:
        if not self.smtp_password_encrypted:
            return ""
        return _fernet().decrypt(self.smtp_password_encrypted.encode()).decode()
