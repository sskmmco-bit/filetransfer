"""One-time downgrade: existing LDAP Uploaders -> Downloader.

The Downloader role is now the default for LDAP-provisioned users (see 0006 and
apps/accounts/ldap_provision.py). This migration aligns already-provisioned LDAP
accounts that still carry the old default (Uploader) with the new policy.

Only LDAP-sourced rows are touched — local accounts an admin set to Uploader on
purpose are left alone. The reverse is a no-op (we can't tell which rows we
moved, and Downloader is the intended state going forward).
"""
from django.db import migrations


def downgrade(apps, schema_editor):
    User = apps.get_model("accounts", "User")
    Role = apps.get_model("accounts", "Role")

    downloader = Role.objects.filter(slug="downloader").first()
    uploader = Role.objects.filter(slug="uploader").first()
    if not (downloader and uploader):
        return

    User.objects.filter(auth_source="ldap", role=uploader).update(role=downloader)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0006_seed_downloader_role"),
    ]

    operations = [
        migrations.RunPython(downgrade, noop),
    ]
