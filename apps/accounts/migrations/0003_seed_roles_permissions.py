"""Seed the permission catalog and the three system roles (§5.7.4).

Idempotent: re-running updates labels and (re)grants the system-role
permission sets without duplicating rows. System roles are flagged is_system
so the admin cannot delete them.
"""
from django.db import migrations

# codename -> (label, category)
PERMISSIONS = {
    "files.upload": ("Upload files", "Files"),
    "files.upload_public": ("Create public links", "Files"),
    "files.download": ("Download files", "Files"),
    "files.delete": ("Delete files", "Files"),
    "files.view_all": ("View all files", "Files"),
    "categories.manage": ("Manage categories", "Files"),
    "users.manage": ("Manage users", "Administration"),
    "roles.manage": ("Manage roles & permissions", "Administration"),
    "groups.manage": ("Manage groups", "Administration"),
    "settings.manage": ("Manage site settings", "Administration"),
    "retention.manage": ("Manage retention policy", "Administration"),
    "audit.view": ("View audit logs", "Administration"),
}

# slug -> (name, description, permission codenames | "*")
ROLES = {
    "superadmin": ("SuperAdmin", "Full access to everything.", "*"),
    "admin": (
        "Admin",
        "Manage users, files, settings and audit; cannot manage roles.",
        [
            "files.upload", "files.upload_public", "files.download", "files.delete",
            "files.view_all", "categories.manage", "users.manage", "groups.manage",
            "settings.manage", "retention.manage", "audit.view",
        ],
    ),
    "uploader": (
        "Uploader",
        "Upload and download files.",
        ["files.upload", "files.download"],
    ),
}


def seed(apps, schema_editor):
    Permission = apps.get_model("accounts", "Permission")
    Role = apps.get_model("accounts", "Role")
    RolePermission = apps.get_model("accounts", "RolePermission")

    perms = {}
    for codename, (label, category) in PERMISSIONS.items():
        obj, _ = Permission.objects.update_or_create(
            codename=codename, defaults={"label": label, "category": category}
        )
        perms[codename] = obj

    for slug, (name, description, codes) in ROLES.items():
        role, _ = Role.objects.update_or_create(
            slug=slug,
            defaults={"name": name, "description": description, "is_system": True},
        )
        wanted = list(perms.values()) if codes == "*" else [perms[c] for c in codes]
        for perm in wanted:
            RolePermission.objects.get_or_create(role=role, permission=perm)


def unseed(apps, schema_editor):
    Role = apps.get_model("accounts", "Role")
    Permission = apps.get_model("accounts", "Permission")
    Role.objects.filter(slug__in=ROLES.keys()).delete()
    Permission.objects.filter(codename__in=PERMISSIONS.keys()).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("accounts", "0002_permission_role_alter_user_role_rolepermission_and_more"),
    ]

    operations = [
        migrations.RunPython(seed, unseed),
    ]
