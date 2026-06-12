# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

MMFileTransfer (`mmftp`) — an internal file-transfer web app. Django 5.1 + Gunicorn behind nginx, PostgreSQL, MinIO (S3-compatible object storage), Redis + Celery (worker + Beat). Everything runs in Docker. English-only, unified user model (no separate client vs. staff accounts).

`ROADMAP.md` is the source of truth for status: **v1 core (Phases 0–5) is complete and verified** — users/roles/auth/LDAP/audit, two-step uploads to MinIO, authorized + public downloads, notifications, admin console, retention/expiry. Phase 6 (2FA, encryption-at-rest) and Phase 7 (hardening) are not started. `MMFileTransfer_Flows_v2.pdf` is the spec; code comments reference its section numbers (e.g. §5.8).

## Commands

Development (autoreload, code bind-mounted):
```bash
cp .env.example .env
docker compose up --build      # postgres, redis, minio, createbuckets, web, worker, beat
```
On boot the `web` container waits for Postgres, runs `makemigrations` + `migrate`, `collectstatic`, and bootstraps a superuser from `DJANGO_SUPERUSER_*` (default `admin` / `adminpass123`).

Production-like (Gunicorn + nginx on :80, no bind-mount, `DEBUG=False`):
```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

Common operations (all via the running container):
```bash
docker compose exec web python manage.py makemigrations   # then COMMIT the files
docker compose exec web python manage.py migrate
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py import_orphans [--dry-run] [--owner USERNAME]
docker compose exec worker celery -A mmftp inspect ping
docker compose logs -f web worker beat
```

Verify Celery end-to-end:
```bash
docker compose exec web python manage.py shell -c \
  "from mmftp.celery import debug_task; print(debug_task.delay().get(timeout=10))"
```

Key URLs: `/` dashboard, `/accounts/login/` (employee ID / email / username), `/admin/` Django admin, `/console/` in-app admin console, `/healthz` (DB + Redis probe), MinIO console at `:9001`.

**There is no automated test suite** — no pytest/conftest, no `tests.py`. Phases were verified manually against a running stack. If you add tests, you are establishing the convention.

## Architecture

### App layout (`apps/`, label = directory name)
- **accounts** — `User` (AbstractUser + `employee_id`, `auth_source`, role FK, quota), the custom permission system, `Group`, `MultiIdentifierBackend`, login security/throttle.
- **core** — dashboard, `/healthz`, `get_client_ip()` (`utils.py`), and the **in-app admin console** (`management.py`, `/console/`).
- **config** — `SiteSettings` singleton + `CustomField` definitions.
- **files** — the heart of the app: `StoredFile`, `UploadSession`, `FileAssignment`, `Category`; upload/download views; domain logic in `services.py`; Celery tasks.
- **notifications** — transactional email (`email.py`, inline SMTP) + deferred Celery email (`tasks.py`) + `NotificationLog`.
- **audit** — `LoginAttempt`, `ActivityLog`, `CronLog`, `DownloadEvent` (read-only admin).
- **public** — token links + email-verified anonymous download (`PublicDownloadVerification`).

### Domain logic lives in services, not views
`apps/files/services.py` holds the transactional core. Views are thin; they call these functions. When changing file behavior, edit services here, not the views:
- `complete_transfer(session)` — assemble + validate uploaded bytes (single-pass SHA-256 + magic-byte sniff + policy), move to `pending_metadata`.
- `activate_file(...)` — copy temp→final key, create assignments, mint public token, enqueue tasks. **Row-locked and idempotent** (a retried activation never double-copies or double-emails).
- `add_recipients`, `reserve_download_slot` (atomic limit enforcement), `soft_delete_stored_file`, `ensure_public_token` / `disable_public_link`.

### File lifecycle (explicit state machine)
```
uploading → pending_metadata → active → deleted
```
"Complete transfer" (bytes written + validated) and "activate" (metadata saved, file goes live) are deliberately **separate steps** — never one "finalize". Blobs: a `temp_key` during upload, copied to final key `files/{year}/{month}/{uuid}-{name}` on activation. `UploadSession` maps a chunked upload to an S3 multipart; abandoned sessions (24h) are purged by a Celery task.

Dual upload path (auto-picked by the vanilla-JS uploader, threshold 50 MiB in `services.CHUNK_THRESHOLD`): direct POST for small files, or `init_upload` → `upload_chunk`×N → `upload_complete` onto MinIO multipart. Soft-delete removes the MinIO object but keeps the row + audit history.

### MinIO / object storage
Bucket is **private**; all delivery uses short-lived **presigned URLs**. Critical: `MINIO_ENDPOINT_URL` (`minio:9000`, in-container) is used for server-side ops, but presigned URLs handed to browsers are minted against `MINIO_PUBLIC_ENDPOINT_URL` because the in-container host isn't resolvable from a user's browser. Storage helpers are in `apps/files/storage.py`.

### Permission system (custom, not Django's)
The app has its **own** `Permission` / `Role` / `RolePermission` catalog in `apps/accounts/models.py`, addressed by dotted codenames (`files.upload`, `audit.view`, `settings.manage`, …) — distinct from `django.contrib.auth.Permission` (which stays bound to admin model CRUD). Check access with `user.has_perm_code("files.upload")`. Django superusers and the SuperAdmin role implicitly hold every permission. System roles (SuperAdmin/Admin/Uploader) and the permission catalog are **seeded by data migration** `accounts/0003_seed_roles_permissions.py` — add new permission codenames there.

### Authentication
`MultiIdentifierBackend` resolves the typed identifier against username/email/employee_id. **Local accounts always win**: if a local row resolves, only its password is tried (no LDAP fallback). Otherwise, if LDAP is enabled in SiteSettings, it binds against the directory and JIT-provisions a new user as Uploader on genuine first login (attribute collision with a local row blocks JIT). Login throttle/hard-block and `LoginAttempt`/`ActivityLog` recording live in `apps/accounts/security.py`, wired into the login view.

### Runtime config: SiteSettings singleton
`apps/config/models.py::SiteSettings.get()` — exactly one row. **Runtime-editable** settings (LDAP, SMTP, retention, quotas, pagination, idle timeout) live here; deployment constants live in `mmftp/settings.py`. Secrets (LDAP bind password, SMTP password) are **Fernet-encrypted at rest** via `SECRETS_ENCRYPTION_KEY` — use `set_/get_ldap_bind_password` and `set_/get_smtp_password`, never the raw `*_encrypted` fields.

### In-app admin console (`/console/`)
`apps/core/management.py` drives generic list/create/edit/delete over a small `Resource` REGISTRY (users, roles, permissions, groups, categories, custom-fields), gated by `user.is_admin` / `is_superadmin`. Site Settings and the Files manager are special-cased. This is separate from Django's `/admin/`.

### Celery
`mmftp/celery.py` auto-discovers `tasks.py` in each app. Beat schedule is defined there: `purge_expired_files` (daily, retention + expiry + abandoned-upload cleanup, writes `CronLog`) and `send_expiry_reminders` (daily). Email has two paths: **inline transactional** (`notifications/email.py`, never Celery — used for public verification codes) and **deferred** (`send_deferred_email` with retry/backoff, at-most-once via `NotificationLog.idempotency_key`).

## Gotchas

- **Migrations are generated at container boot** for convenience, but you must run `makemigrations` and **commit the migration files** for real work — don't rely on boot-time generation.
- **Restart `worker` and `beat` after changing task code** — Celery has no hot-reload (the web container does autoreload).
- `ldap3` and `cryptography` are imported lazily; the app runs without them until an LDAP-auth or encrypted-secret path executes. (A corporate TLS-intercepting proxy has historically blocked installing these in the image — see ROADMAP Phase 1.)
- `SECRETS_ENCRYPTION_KEY` is empty by default; generate one before wiring real SMTP/LDAP secrets: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Content policy is currently a **disallowed-extension blocklist** (empty by default — all types accepted, executables included); tighten `DISALLOWED_EXTENSIONS` in `services.py` per deployment.
- The repo root contains a stray `nul` file and `static/`/`staticfiles/` — `staticfiles/` is collected output; put source static assets in `static/`.
