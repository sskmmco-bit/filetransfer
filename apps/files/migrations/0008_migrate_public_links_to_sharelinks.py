"""Convert legacy per-file public links (shared `public_token` bundles) into
first-class ShareLink + ShareLinkFile rows. Idempotent: skips tokens that
already have a ShareLink.
"""
from django.db import migrations


def forward(apps, schema_editor):
    StoredFile = apps.get_model("files", "StoredFile")
    ShareLink = apps.get_model("files", "ShareLink")
    ShareLinkFile = apps.get_model("files", "ShareLinkFile")

    tokens = (
        StoredFile.objects.filter(is_public=True)
        .exclude(public_token="")
        .values_list("public_token", flat=True)
        .distinct()
    )
    for token in tokens:
        if ShareLink.objects.filter(token=token).exists():
            continue
        bundle = list(
            StoredFile.objects.filter(public_token=token).order_by("created_at")
        )
        if not bundle:
            continue
        rep = bundle[0]
        n = len(bundle)
        link = ShareLink.objects.create(
            token=token,
            name=f"Link to {n} file{'s' if n != 1 else ''}",
            created_by_id=rep.owner_id,
            require_email_verify=rep.public_require_email_verify,
            allow_download=rep.public_allow_download,
            password_hash=rep.public_password_hash,
            expires_at=rep.expiry_date,
            download_limit=rep.download_limit,
            download_count=0,
            view_count=0,
            is_active=True,
            created_at=rep.created_at,
        )
        ShareLinkFile.objects.bulk_create(
            [ShareLinkFile(share_link=link, stored_file=f) for f in bundle]
        )


def backward(apps, schema_editor):
    # Non-destructive rollback: drop the generated links (per-file columns kept).
    ShareLink = apps.get_model("files", "ShareLink")
    ShareLink.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("files", "0007_sharelink_sharelinkfile_sharelink_files_and_more"),
    ]
    operations = [migrations.RunPython(forward, backward)]
