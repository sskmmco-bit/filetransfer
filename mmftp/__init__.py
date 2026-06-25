# Background work runs as Django management commands on systemd timers
# (apps.*.jobs + apps/*/management/commands), not Celery — no app object to load here.
