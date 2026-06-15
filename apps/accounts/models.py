"""Accounts domain model (§2.4, §5.7.x).

AUTH_USER_MODEL = accounts.User is set before the first migration (§3) so every
FK binds to it correctly. Phase 1 replaces the temporary `role` string with a
real Role / Permission / RolePermission permission system; system roles
(SuperAdmin, Admin, Uploader) are seeded by a data migration.
"""
from __future__ import annotations

from django.contrib.auth.models import AbstractUser
from django.db import models


class AuthSource(models.TextChoices):
    LOCAL = "local", "Local"
    LDAP = "ldap", "LDAP"


# Slugs for the seeded system roles. Code that needs to reference a role by
# identity (e.g. JIT provisioning, the superuser bootstrap) uses these.
class RoleSlug(models.TextChoices):
    SUPERADMIN = "superadmin", "SuperAdmin"
    ADMIN = "admin", "Admin"
    UPLOADER = "uploader", "Uploader"


class Permission(models.Model):
    """A single capability, addressed by a stable dotted codename.

    This is the app's own permission catalog (§5.7.4) — distinct from Django's
    built-in auth.Permission, which stays bound to model CRUD in the admin.
    """

    codename = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=128)
    category = models.CharField(max_length=64, blank=True)

    class Meta:
        db_table = "accounts_permission"
        ordering = ("category", "codename")

    def __str__(self):
        return self.codename


class Role(models.Model):
    """A named bundle of permissions (§2.4). System roles cannot be deleted."""

    slug = models.SlugField(max_length=32, unique=True)
    name = models.CharField(max_length=64, unique=True)
    description = models.CharField(max_length=255, blank=True)
    is_system = models.BooleanField(default=False)
    permissions = models.ManyToManyField(
        Permission, through="RolePermission", related_name="roles", blank=True
    )

    class Meta:
        db_table = "accounts_role"
        ordering = ("name",)

    def __str__(self):
        return self.name

    def permission_codes(self) -> set[str]:
        return set(self.permissions.values_list("codename", flat=True))


class RolePermission(models.Model):
    """Explicit through table for Role <-> Permission (§5.7.4)."""

    role = models.ForeignKey(Role, on_delete=models.CASCADE)
    permission = models.ForeignKey(Permission, on_delete=models.CASCADE)

    class Meta:
        db_table = "accounts_role_permission"
        unique_together = (("role", "permission"),)

    def __str__(self):
        return f"{self.role.slug}:{self.permission.codename}"


class Group(models.Model):
    """A named set of users, for assigning a file to many recipients at once."""

    name = models.CharField(max_length=120, unique=True)
    description = models.CharField(max_length=255, blank=True)
    members = models.ManyToManyField(
        "accounts.User", related_name="file_groups", blank=True
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "accounts_group"
        ordering = ("name",)

    def __str__(self):
        return self.name


class User(AbstractUser):
    """Unified user model — no separate client vs system accounts (§1.1)."""

    # Identity / auth
    employee_id = models.CharField(max_length=64, blank=True, null=True, unique=True)
    auth_source = models.CharField(
        max_length=8, choices=AuthSource.choices, default=AuthSource.LOCAL
    )

    # Application role (replaces the Phase-0 starter `role` string).
    role = models.ForeignKey(
        Role,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="users",
    )

    # Profile (§5.7.2)
    phone = models.CharField(max_length=32, blank=True)
    address = models.CharField(max_length=255, blank=True)
    contact = models.CharField(max_length=255, blank=True)

    # Behaviour flags
    must_change_password = models.BooleanField(default=False)
    notify_on_assignment = models.BooleanField(default=True)
    can_upload_public = models.BooleanField(default=False)

    # Storage quota (§quota). Bytes. NULL => inherit the site default; an explicit
    # 0 means unlimited (overrides the default).
    quota_bytes = models.BigIntegerField(null=True, blank=True)

    class Meta:
        db_table = "accounts_user"

    def __str__(self):
        return self.get_full_name() or self.username

    @property
    def is_ldap(self) -> bool:
        return self.auth_source == AuthSource.LDAP

    @property
    def role_slug(self) -> str | None:
        return self.role.slug if self.role_id else None

    @property
    def is_superadmin(self) -> bool:
        return self.role_slug == RoleSlug.SUPERADMIN

    @property
    def is_admin(self) -> bool:
        return self.role_slug in (RoleSlug.SUPERADMIN, RoleSlug.ADMIN)

    def has_perm_code(self, codename: str) -> bool:
        """App-level permission check (§5.7.4).

        Django superusers and the SuperAdmin role implicitly hold every
        permission; otherwise the user's role must grant the codename.
        """
        if self.is_superuser or self.is_superadmin:
            return True
        if not self.role_id:
            return False
        return self.role.permissions.filter(codename=codename).exists()

    # ----- Storage quota (§quota) -----
    def storage_used_bytes(self) -> int:
        """Bytes counted against this user's quota — their own live files.

        Excludes TRASHED as well as DELETED: once a user sends a file to trash
        they've relinquished it (it leaves their lists, public links stop
        resolving, and only an admin can restore or purge it), so it must not
        keep burning their quota. The trashed object still occupies real disk
        until purged — that's the admin's system-storage concern, not the
        user's quota. ACTIVE + in-flight drafts (PENDING_METADATA/UPLOADING)
        still count so trash/draft churn can't be used to dodge the limit.
        """
        from django.db.models import Sum

        from apps.files.models import FileStatus, StoredFile

        return (
            StoredFile.objects.filter(owner=self)
            .exclude(status__in=[FileStatus.TRASHED, FileStatus.DELETED])
            .aggregate(s=Sum("size"))["s"]
            or 0
        )

    def quota_effective_bytes(self):
        """Resolved quota in bytes, or None for unlimited.

        Explicit quota_bytes wins (0 => unlimited). NULL inherits the site
        default; a default of 0 GB also means unlimited.
        """
        from apps.config.models import SiteSettings

        raw = self.quota_bytes
        if raw is None:
            raw = SiteSettings.get().default_user_quota_gb * (1024 ** 3)
        return None if raw <= 0 else raw

    def quota_remaining_bytes(self):
        """Bytes left before hitting the quota, or None if unlimited."""
        eff = self.quota_effective_bytes()
        if eff is None:
            return None
        return max(0, eff - self.storage_used_bytes())

    def quota_pct(self) -> int:
        """Percentage of quota used (0 when unlimited)."""
        eff = self.quota_effective_bytes()
        if not eff:
            return 0
        return min(100, round(self.storage_used_bytes() / eff * 100))

    def storage_label(self) -> str:
        """Human 'used / quota' string for admin lists (e.g. '1.2 GB / 50 GB')."""
        from apps.core.views import _human_size

        used = _human_size(self.storage_used_bytes())
        eff = self.quota_effective_bytes()
        return f"{used} / {_human_size(eff)}" if eff else f"{used} / ∞"
