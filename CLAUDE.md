# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MMFileTransfer (`mmftp`) — an internal file-transfer web app. Django 5.1 + Gunicorn behind nginx, PostgreSQL, MinIO (S3-compatible object storage), Redis (Django cache). Background work runs as Django management commands fired by **systemd timers** (no Celery/broker). Runs directly on a Linux host (no Docker): Postgres, Redis, and nginx as OS packages; MinIO and Gunicorn as `systemd` units, plus the job timers. English-only, unified user model (no separate client vs. staff accounts).

`ROADMAP.md` is the source of truth for status: **v1 core (Phases 0–5) is complete and verified** — users/roles/auth/LDAP/audit, two-step uploads to MinIO, authorized + public downloads, notifications, admin console, retention/expiry. Phase 6 (2FA, encryption-at-rest) and Phase 7 (hardening) are not started. `MMFileTransfer_Flows_v2.pdf` is the spec; code comments reference its section numbers (e.g. §5.8).

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

Local development (Postgres, Redis, and MinIO must be running on the host — see
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
python manage.py send_queued_notifications  # drain the deferred-email queue
python manage.py purge_expired_files        # retention/expiry/abandoned cleanup
python manage.py send_expiry_reminders      # queue expiry reminders
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

Key URLs: `/` dashboard, `/accounts/login/` (employee ID / email / username), `/admin/` Django admin, `/console/` in-app admin console, `/healthz` (DB + Redis probe), MinIO console at `:9001`.

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
- `activate_file(...)` — copy temp→final key, create assignments, mint public token, generate the thumbnail inline + queue notification emails. **Row-locked and idempotent** (a retried activation never double-copies or double-emails).
- `add_recipients`, `reserve_download_slot` (atomic limit enforcement), `soft_delete_stored_file`, `ensure_public_token` / `disable_public_link`.

### File lifecycle (explicit state machine)
```
uploading → pending_metadata → active → deleted
```
"Complete transfer" (bytes written + validated) and "activate" (metadata saved, file goes live) are deliberately **separate steps** — never one "finalize". Blobs: a `temp_key` during upload, copied to final key `files/{year}/{month}/{uuid}-{name}` on activation. `UploadSession` maps a chunked upload to an S3 multipart; abandoned sessions (24h) are purged by the daily `purge_expired_files` command.

Dual upload path (auto-picked by the vanilla-JS uploader, threshold 50 MiB in `services.CHUNK_THRESHOLD`): direct POST for small files, or `init_upload` → `upload_chunk`×N → `upload_complete` onto MinIO multipart. Soft-delete removes the MinIO object but keeps the row + audit history.

### MinIO / object storage
Bucket is **private**; all delivery uses short-lived **presigned URLs**. Critical: `MINIO_ENDPOINT_URL` (`127.0.0.1:9000`, loopback on the server) is used for server-side ops, but presigned URLs handed to browsers are minted against `MINIO_PUBLIC_ENDPOINT_URL` because the loopback address isn't reachable from a user's browser — nginx proxies `/<bucket>/` through to MinIO. Storage helpers are in `apps/files/storage.py`.

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

Email has two paths: **inline transactional** (`notifications/email.py`, never queued — used for public verification codes) and **deferred** (queued in `NotificationLog`, drained by the timer, at-most-once via `idempotency_key`). The SMTP connection carries a 30 s timeout that must stay below the claim stale bound so a slow-but-live send can't be re-claimed and double-sent. Thumbnails are generated **inline** on activation (no job).

## Gotchas

- **Migrations must be generated and committed** — run `makemigrations` locally and commit the files. A server install applies committed migrations only; it does not generate them (the `.env` sets `DJANGO_SKIP_MAKEMIGRATIONS=1`).
- **Background jobs pick up code changes automatically** — the systemd timers re-exec `manage.py` each run, so no restart is needed after editing `jobs.py` or a command (unlike the old Celery worker/beat). Only `mmftp-web` (Gunicorn) needs a restart on a server.
- **Thumbnails are generated synchronously on activation** (`apps/files/jobs.generate_thumbnail`, called inline from `services._enqueue_post_activation`) — it adds latency to image activations and runs on the request thread; sources over `THUMBNAIL_MAX_SOURCE_BYTES` (25 MiB) are skipped.
- `ldap3` and `cryptography` are imported lazily; the app runs without them until an LDAP-auth or encrypted-secret path executes. (A corporate TLS-intercepting proxy has historically blocked installing these via pip — see ROADMAP Phase 1.)
- `SECRETS_ENCRYPTION_KEY` is empty by default; generate one before wiring real SMTP/LDAP secrets: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Content policy is currently a **disallowed-extension blocklist** (empty by default — all types accepted, executables included); tighten `DISALLOWED_EXTENSIONS` in `services.py` per deployment.
- The repo root contains a stray `nul` file and `static/`/`staticfiles/` — `staticfiles/` is collected output; put source static assets in `static/`.
