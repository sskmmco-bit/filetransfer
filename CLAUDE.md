# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MMFileTransfer (`mmftp`) — an internal file-transfer web app. Django 5.1 + Gunicorn behind nginx, PostgreSQL, and **local-disk file storage** (file blobs live on the app host under `FILE_STORAGE_ROOT`; no MinIO/S3). The Django cache is in-process (`LocMemCache`) — no Redis. Background work runs as Django management commands fired by **systemd timers** (no Celery/broker). Runs directly on a Linux host (no Docker): Postgres and nginx as OS packages; Gunicorn as a `systemd` unit, plus the job timers. English-only, unified user model (no separate client vs. staff accounts).

`ROADMAP.md` is the source of truth for status: **v1 core (Phases 0–5) is complete and verified** — users/roles/auth/LDAP/audit, two-step uploads to local disk, authorized + public downloads, notifications, admin console, retention/expiry. Phase 6 (2FA, encryption-at-rest) and Phase 7 (hardening) are not started. `MMFileTransfer_Flows_v2.pdf` is the spec; code comments reference its section numbers (e.g. §5.8).

## Workflow rules (MANDATORY)

These rules govern **every** code/config change in this repo. They are not optional and override default behavior.

### 1. Plan before changing — and wait for approval

Before making **any** change, create a plan file in the `plans/` folder. The filename **must** be prefixed with the date and time the plan was created, in the format `yyyymmdd_hhmmss`, followed by a short kebab-case slug — e.g. `plans/20260617_143022_add-2fa-login.md`.

The plan file **must** contain:

- **Status** — one of `planned` / `in-progress` / `completed`. Starts as `planned`.
- **Summary** — what is going to be changed and why.
- **Files affected** — an explicit list, each tagged `created` / `changed` / `deleted`.
- **Database changes** — new/altered/removed models, fields, migrations, data migrations, or indexes; "none" if not applicable.
- **Effect on the current system** — runtime/behavioral impact, backward-compatibility, services to restart (e.g. `mmftp-worker`/`mmftp-beat`), config/secrets needed, and rollback considerations.

**After the plan file is written, STOP and get explicit manual approval from the user before implementing anything.** Do not begin edits until the user approves the plan. When work begins, update **Status** to `in-progress`; once the change is fully done, update **Status** to `completed`.

### 2. Log every change

Whenever a change **is made**, create a change-log file in the `change_log/` folder. The filename **must** be prefixed with the date and time in the format `yyyymmdd_hhmmss`, followed by a short kebab-case slug — e.g. `change_log/20260617_151045_add-2fa-login.md`.

The change-log file should record what was actually changed (files, DB migrations, behavior), reference the originating plan file, and note any deviations from the plan.

## Commands

Local development (only Postgres must be running — via `docker compose -f docker-compose.dev.yml up -d` locally, or on the host; no Redis, no MinIO — file blobs go to a local dir, `FILE_STORAGE_ROOT`, default `<repo>/media/files`; see
`deploy/INSTALL_NO_DOCKER.md` for installing them). Create a venv, install deps,
and point `.env` at the local services:
```bash
python -m venv venv
venv/bin/pip install -r requirements.txt   # requirements-dev.txt adds locust + tooling
cp .env.example .env                        # then edit hosts/secrets to taste
set -a; source .env; set +a                 # load env into the shell
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py runserver                  # dev autoreload
# Background jobs are management commands — run them on demand in dev:
python manage.py send_queued_notifications    # drain the deferred-email queue
python manage.py purge_expired_files          # retention/expiry/abandoned cleanup
python manage.py send_expiry_reminders        # queue expiry reminders
python manage.py generate_pending_thumbnails  # thumbnail new images (async on a timer in prod)
```

Production-like (Gunicorn + nginx on :80, `DEBUG=False`): the web app runs as the
`mmftp-web` `systemd` unit behind host nginx; background jobs run as oneshot
management commands fired by `systemd` **timers** (`mmftp-notifications.timer`
~2 min, `mmftp-purge.timer` daily, `mmftp-reminders.timer` daily). The full
procedure — and a one-command installer (`deploy/install_no_docker.sh`) — is in
`deploy/INSTALL_NO_DOCKER.md`.

Common operations (with the venv active and `.env` loaded):
```bash
python manage.py makemigrations   # then COMMIT the files
python manage.py migrate
python manage.py createsuperuser
python manage.py import_orphans [--dry-run] [--owner USERNAME]
python manage.py send_queued_notifications   # drain deferred-email queue on demand
```

On a server install, manage the web service and inspect the job timers via systemd:
```bash
sudo systemctl restart mmftp-web                 # job timers re-exec code each run
journalctl -u mmftp-web -f
systemctl list-timers 'mmftp-*' --no-pager       # next/last run of each job
sudo systemctl start mmftp-notifications.service  # run a job now (don't wait for timer)
journalctl -u mmftp-notifications -n 50           # that run's output
```

Key URLs: `/` dashboard, `/accounts/login/` (employee ID / email / username), `/admin/` Django admin, `/console/` in-app admin console, `/healthz` (DB + cache + storage probe).

**There is no automated test suite** — no pytest/conftest, no `tests.py`. Phases were verified manually against a running stack. If you add tests, you are establishing the convention.

## Architecture

### App layout (`apps/`, label = directory name)
- **accounts** — `User` (AbstractUser + `employee_id`, `auth_source`, role FK, quota), the custom permission system, `Group`, `MultiIdentifierBackend`, login security/throttle.
- **core** — dashboard, `/healthz`, `get_client_ip()` (`utils.py`), and the **in-app admin console** (`management.py`, `/console/`).
- **config** — `SiteSettings` singleton + `CustomField` definitions.
- **files** — the heart of the app: `StoredFile`, `UploadSession`, `FileAssignment`, `Category`; upload/download views; domain logic in `services.py`; background jobs in `jobs.py` (thumbnail, purge).
- **notifications** — transactional email (`email.py`, inline SMTP) + deferred email queue + drain (`jobs.py`, run by `send_queued_notifications`) + `NotificationLog`.
- **audit** — `LoginAttempt`, `ActivityLog`, `CronLog`, `DownloadEvent` (read-only admin).
- **public** — token links + email-verified anonymous download (`PublicDownloadVerification`).

### Domain logic lives in services, not views
`apps/files/services.py` holds the transactional core. Views are thin; they call these functions. When changing file behavior, edit services here, not the views:
- `complete_transfer(session)` — assemble + validate uploaded bytes (single-pass SHA-256 + magic-byte sniff + policy), move to `pending_metadata`.
- `activate_file(...)` — copy temp→final key, create assignments, mint public token, queue notification emails (thumbnailing is deferred to the `generate_pending_thumbnails` timer, not done here). **Row-locked and idempotent** (a retried activation never double-copies or double-emails).
- `add_recipients`, `reserve_download_slot` (atomic limit enforcement), `soft_delete_stored_file`, `ensure_public_token` / `disable_public_link`.

### File lifecycle (explicit state machine)
```
uploading → pending_metadata → active → deleted
```
"Complete transfer" (bytes written + validated) and "activate" (metadata saved, file goes live) are deliberately **separate steps** — never one "finalize". Blobs: a `temp_key` during upload, copied to final key `files/{year}/{month}/{uuid}-{name}` on activation. `UploadSession` maps a chunked upload to an on-disk append (the "multipart"); abandoned sessions (24h) are purged by the daily `purge_expired_files` command.

Dual upload path (auto-picked by the vanilla-JS uploader, threshold 50 MiB in `services.CHUNK_THRESHOLD`): direct POST for small files, or `init_upload` → `upload_chunk`×N → `upload_complete`; chunked parts are appended (in order) to a single temp file on disk. Soft-delete removes the on-disk blob but keeps the row + audit history.

### File storage (local disk)
File blobs live on the app host's local disk under `FILE_STORAGE_ROOT` (keys are relative paths: `temp/…`, `files/…`, `thumbnails/…`). There is **no MinIO/S3 and no presigned URLs**. Delivery: the view authorizes the request, then `storage.serve(key, …)` returns the bytes — in production via an nginx `X-Accel-Redirect` into an `internal` `/_protected/` location aliased to `FILE_STORAGE_ROOT` (`FILE_STORAGE_USE_X_ACCEL=True`), in dev via a Django `FileResponse`. Every byte served passes through app authz (no replayable bearer URLs). All storage helpers — `put_object`, `create_multipart`/`upload_part`/`complete_multipart`/`abort_multipart`, `copy_object`, `delete_object`, `object_exists`, `get_object_body`, and `serve` — are in `apps/files/storage.py`; `_safe_path` blocks keys that escape the root.

### Permission system (custom, not Django's)
The app has its **own** `Permission` / `Role` / `RolePermission` catalog in `apps/accounts/models.py`, addressed by dotted codenames (`files.upload`, `audit.view`, `settings.manage`, …) — distinct from `django.contrib.auth.Permission` (which stays bound to admin model CRUD). Check access with `user.has_perm_code("files.upload")`. Django superusers and the SuperAdmin role implicitly hold every permission. System roles (SuperAdmin/Admin/Uploader) and the permission catalog are **seeded by data migration** `accounts/0003_seed_roles_permissions.py` — add new permission codenames there.

### Authentication
`MultiIdentifierBackend` resolves the typed identifier against username/email/employee_id. **Local accounts always win**: if a local row resolves, only its password is tried (no LDAP fallback). Otherwise, if LDAP is enabled in SiteSettings, it binds against the directory and JIT-provisions a new user as Uploader on genuine first login (attribute collision with a local row blocks JIT). Login throttle/hard-block and `LoginAttempt`/`ActivityLog` recording live in `apps/accounts/security.py`, wired into the login view.

### Runtime config: SiteSettings singleton
`apps/config/models.py::SiteSettings.get()` — exactly one row. **Runtime-editable** settings (LDAP, SMTP, retention, quotas, pagination, idle timeout) live here; deployment constants live in `mmftp/settings.py`. Secrets (LDAP bind password, SMTP password) are **Fernet-encrypted at rest** via `SECRETS_ENCRYPTION_KEY` — use `set_/get_ldap_bind_password` and `set_/get_smtp_password`, never the raw `*_encrypted` fields.

### In-app admin console (`/console/`)
`apps/core/management.py` drives generic list/create/edit/delete over a small `Resource` REGISTRY (users, roles, permissions, groups, categories, custom-fields), gated by `user.is_admin` / `is_superadmin`. Site Settings and the Files manager are special-cased. This is separate from Django's `/admin/`.

### Background jobs (no Celery)
Background work is plain functions in each app's `jobs.py`, invoked either inline or by Django management commands on **systemd timers** (see `deploy/INSTALL_NO_DOCKER.md`):
- `send_queued_notifications` (timer ~2 min) — drains the deferred-email queue. It **claims** `NotificationLog` rows under a short `select_for_update(skip_locked=True)` lock (status→`sending` + `claimed_at`), then sends SMTP **outside** the lock; overlap-safe. A row stuck in `sending` past `NOTIFICATION_CLAIM_STALE_MINUTES` (15) is re-claimed; failures retry each run up to `MAX_RETRIES` then `failed`. Logic in `apps/notifications/jobs.py`.
- `purge_expired_files` (daily) — retention + expiry + abandoned-upload cleanup, writes `CronLog`. Logic in `apps/files/jobs.py`.
- `send_expiry_reminders` (daily) — queues expiry reminders.
- `generate_pending_thumbnails` (timer ~2 min) — thumbnails active images that have no `thumbnail_key` yet, off the request thread; bumps `thumbnail_attempts` and stops after `THUMBNAIL_MAX_ATTEMPTS` (3). Logic in `apps/files/jobs.py`.

Email has two paths: **inline transactional** (`notifications/email.py`, never queued — used for public verification codes) and **deferred** (queued in `NotificationLog`, drained by the timer, at-most-once via `idempotency_key`). The SMTP connection carries a 30 s timeout that must stay below the claim stale bound so a slow-but-live send can't be re-claimed and double-sent. Thumbnails are generated **asynchronously** by the `generate_pending_thumbnails` timer (not inline on activation).

## Gotchas

- **Migrations must be generated and committed** — run `makemigrations` locally and commit the files. A server install applies committed migrations only; it does not generate them (the `.env` sets `DJANGO_SKIP_MAKEMIGRATIONS=1`).
- **Background jobs pick up code changes automatically** — the systemd timers re-exec `manage.py` each run, so no restart is needed after editing `jobs.py` or a command (unlike the old Celery worker/beat). Only `mmftp-web` (Gunicorn) needs a restart on a server.
- **Thumbnails are generated asynchronously** by the `generate_pending_thumbnails` job (systemd timer ~2 min), so a new image's thumbnail appears within a minute or two of activation (the UI's `<img onerror>` falls back to an extension chip until then). `activate_file`/`_enqueue_post_activation` no longer block on thumbnailing. Sources over `THUMBNAIL_MAX_SOURCE_BYTES` (25 MiB) are skipped; permanently-failing images stop after `THUMBNAIL_MAX_ATTEMPTS` (3).
- `ldap3` and `cryptography` are imported lazily; the app runs without them until an LDAP-auth or encrypted-secret path executes. (A corporate TLS-intercepting proxy has historically blocked installing these via pip — see ROADMAP Phase 1.)
- `SECRETS_ENCRYPTION_KEY` is empty by default; generate one before wiring real SMTP/LDAP secrets: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Content policy is currently a **disallowed-extension blocklist** (empty by default — all types accepted, executables included); tighten `DISALLOWED_EXTENSIONS` in `services.py` per deployment.
- The repo root contains a stray `nul` file and `static/`/`staticfiles/` — `staticfiles/` is collected output; put source static assets in `static/`.
