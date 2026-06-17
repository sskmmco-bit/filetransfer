"""Seed the Downloader system role — download-only access (§5.7.4).

Mirrors the style of 0003: idempotent, flags the role is_system so it cannot be
deleted from the console. The Downloader holds exactly one permission,
``files.download`` — it cannot upload, delete, or administer anything. New LDAP
users are provisioned into this role (see apps/accounts/ldap_provision.py).
"""
from django.db import migrations

SLUG = "downloader"
NAME = "Downloader"
DESCRIPTION = "Download files only — cannot upload or administer."
PERMISSION_CODENAMES = ["files.download"]


def seed(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    role, _ = Role.objects.update_or_create(
        slug=SLUG,
        defaults={"name": NAME, "description": DESCRIPTION, "is_system": True},
    )
    for codename in PERMISSION_CODENAMES:
        perm = Permission.objects.filter(codename=codename).first()
        if perm:
            RolePermission.objects.get_or_create(role=role, permission=perm)


def unseed(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Role.objects.filter(slug=SLUG).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0003_seed_roles_permissions"),
        ("accounts", "0005_user_quota_bytes"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
