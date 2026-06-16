from django.db import migrations, models


def set_default_quota_2gb(apps, schema_editor):
    """Bump the live singleton from the old 0 (unlimited) default to 2 GB.

    Only touches rows still on the old default so an admin who has deliberately
    set a different site quota (including an explicit 0 = unlimited later on)
    isn't clobbered by the migration.
    """
    SiteSettings = apps.get_model("config", "SiteSettings")
    SiteSettings.objects.filter(default_user_quota_gb=0).update(default_user_quota_gb=2)


def revert_default_quota(apps, schema_editor):
    SiteSettings = apps.get_model("config", "SiteSettings")
    SiteSettings.objects.filter(default_user_quota_gb=2).update(default_user_quota_gb=0)


class Migration(migrations.Migration):

    dependencies = [
        ('config', '0005_sitesettings_default_user_quota_gb'),
    ]

    operations = [
        migrations.AlterField(
            model_name='sitesettings',
            name='default_user_quota_gb',
            field=models.PositiveIntegerField(default=2),
        ),
        migrations.RunPython(set_default_quota_2gb, revert_default_quota),
    ]
