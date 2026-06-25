# Claim-marking for the DB-backed deferred-email queue (Celery removal).
# Adds the SENDING status choice + claimed_at so the send_queued_notifications
# drain can claim rows under a short lock and send outside it.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("notifications", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="notificationlog",
            name="claimed_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="notificationlog",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("sending", "Sending"),
                    ("sent", "Sent"),
                    ("failed", "Failed"),
                ],
                default="pending",
                max_length=10,
            ),
        ),
    ]
