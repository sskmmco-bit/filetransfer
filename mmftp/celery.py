"""Celery application for MMFileTransfer.

Workers run deferred notifications, the retention/expiry purge, and thumbnail
generation (§3, §5.11, §5.6.2). Celery Beat schedules the periodic tasks.
"""
import os

from celery import Celery
from celery.schedules import crontab

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "mmftp.settings")

app = Celery("mmftp")

# Read config from Django settings, namespaced CELERY_*.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Auto-discover tasks.py in every installed app.
app.autodiscover_tasks()

# Periodic schedule (§5.6.2). The purge task lives in apps.files.tasks as of
# Phase 2 and runs the abandoned-upload cleanup; retention/expiry steps are
# enabled in Phase 5.
app.conf.beat_schedule = {
    "purge-expired-files-daily": {
        "task": "apps.files.tasks.purge_expired_files",
        "schedule": crontab(hour=3, minute=0),
    },
    # Phase 4: notify owners/recipients of files expiring within the window.
    "expiry-reminders-daily": {
        "task": "apps.notifications.tasks.send_expiry_reminders",
        "schedule": crontab(hour=7, minute=0),
    },
}


@app.task(bind=True, ignore_result=True)
def debug_task(self):
    print(f"Request: {self.request!r}")
