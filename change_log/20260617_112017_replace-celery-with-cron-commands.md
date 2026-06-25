# Change log — Replace Celery with management commands + systemd timers

**Plan:** `plans/20260617_103718_replace-celery-with-cron-commands.md`
**Date:** 2026-06-17

## What was changed

Removed the Celery + Redis-broker background layer and re-implemented the four
background jobs as the plan specified: a DB-backed deferred-email queue drained by
a management command on a systemd timer, two daily management commands, and one
job (thumbnails) moved to synchronous in-request execution. Redis is kept as the
Django cache only.

### Database
- `apps/notifications/models.py` — `NotificationStatus` gained `SENDING`;
  `NotificationLog` gained `claimed_at` (nullable).
- Migration `apps/notifications/migrations/0002_notificationlog_claim.py`
  (hand-written: `AddField claimed_at` + `AlterField status` choices). No data
  migration. **Must be applied with `manage.py migrate` on deploy.**

### Renamed modules (Celery tasks → plain functions)
- `apps/notifications/tasks.py` → `apps/notifications/jobs.py`
  - `send_deferred_email` (Celery) → per-row `_send_claimed` + two-phase
    `drain_pending()` (claim under `select_for_update(skip_locked=True)` → mark
    `SENDING`/`claimed_at` → send outside the lock). Stale-claim reclaim via
    `NOTIFICATION_CLAIM_STALE_MINUTES=15`. Retry each run to `MAX_RETRIES=3` then
    `FAILED`. Writes a `CronLog` row when anything was processed.
  - `_enqueue` no longer calls `.delay()`; resets only `FAILED`/`PENDING` rows,
    never a `SENDING` row (prevents double-send).
  - Removed the `ping` task. Kept `enqueue_assignment_email`,
    `enqueue_welcome_email`, `enqueue_expiry_reminder`, `send_expiry_reminders`.
- `apps/files/tasks.py` → `apps/files/jobs.py`
  - `generate_thumbnail` / `purge_expired_files` are plain functions (no
    `@shared_task`). `generate_thumbnail` skips sources >
    `THUMBNAIL_MAX_SOURCE_BYTES=25 MiB`.

### Import-site + behavior updates
- `apps/files/services.py` — imports point at `*.jobs`; `_enqueue_post_activation`
  calls `generate_thumbnail(pk)` **synchronously** (was `.delay()`); docstring updated.
- `apps/accounts/ldap_provision.py` — import → `apps.notifications.jobs`.
- `apps/notifications/email.py` — docstring fixed; added `SMTP_TIMEOUT_SECONDS=30`
  passed to `get_connection(..., timeout=...)`.

### New management commands
- `apps/notifications/management/commands/send_queued_notifications.py` (+ package `__init__`s)
- `apps/notifications/management/commands/send_expiry_reminders.py`
- `apps/files/management/commands/purge_expired_files.py`

### Celery removal
- Deleted `mmftp/celery.py`; `mmftp/__init__.py` no longer imports `celery_app`.
- `mmftp/settings.py` — removed the `CELERY_*` block (kept `REDIS_URL`/`CACHES`);
  updated the email section comment.
- `requirements.txt` — removed `celery==5.4.0` (kept `redis`, `django-redis`).

### Deploy
- `deploy/install_no_docker.sh`, `deploy/wsl_test_install.sh` — replaced the
  `mmftp-worker`/`mmftp-beat` units with a oneshot+timer set
  (`mmftp-notifications` ~2 min, `mmftp-purge` daily 03:00, `mmftp-reminders`
  daily 07:00) + a shared `mmftp-onfailure@.service` journald logger; status
  now uses `systemctl list-timers`.
- `deploy/update_no_docker.sh` — retires legacy `mmftp-worker`/`mmftp-beat` and
  (re)installs the timers idempotently; restarts only `mmftp-web`.
- `deploy/.env.production.template`, `.env.example` — dropped `CELERY_*` vars.
- `deploy/INSTALL_NO_DOCKER.md` — service table, STEP 9/10, day-to-day,
  troubleshooting, HTTPS sections rewritten for timers.

### Docs
- `CLAUDE.md` — intro, command blocks, "### Background jobs (no Celery)" section,
  gotchas (no worker/beat restart; synchronous thumbnail) updated.
- `README.md` — intro, dev job commands, server/timer ops, starter feature list.
- `apps/audit/models.py`, `mmftp/settings.py` comments de-Celery'd.
- `ROADMAP.md` — added a dated note that Celery was removed (historical phase
  entries left intact).

## Verification
- `python -m py_compile` passed on all new/changed Python files.
- Grep confirms no remaining imports of `notifications.tasks` / `files.tasks` /
  `mmftp.celery` / `shared_task` / `celery_app`.
- Not run here (no DB/venv in this environment): `manage.py migrate`,
  `manage.py check`, and an end-to-end drain. **To do on deploy.**

## Deviations from the plan
- Also updated files not enumerated in the plan, for consistency: `.env.example`,
  `apps/audit/models.py` (docstring), `mmftp/settings.py` email comment, and a
  dated note in `ROADMAP.md`.
- `drain_pending` writes a `CronLog` row **only when rows were processed** (not on
  every empty 2-min tick) to avoid flooding `audit_cron_log`; liveness/failures are
  still observable via `systemctl list-timers`, `journalctl`, and the
  `OnFailure=` logger. The plan said "one CronLog row per run".
