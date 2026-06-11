# MMFileTransfer — Build Roadmap

Companion to `MMFileTransfer_Flows_v2.pdf`. Tracks what is built vs. what remains.
v1 core = Phases 0–5; v1 security extensions = Phase 6; Phase 7 = hardening.

Status legend: ✅ done · 🟡 partial · ⬜ not started

---

## Current state (audited)

| Phase | Focus | Status |
|-------|-------|--------|
| 0 | Project setup | ✅ Done |
| 1 | Foundation (users/roles/auth/LDAP/audit) | ✅ Done (LDAP/Fernet runtime pending image rebuild) |
| 2 | Files core (upload, state machine, activate) | ✅ Done & verified |
| 3 | Download & public links | ✅ Done & verified |
| 4 | Notifications | ✅ Done & verified |
| 5 | Admin, groups, fields, retention | ✅ Done & verified — v1 core complete |
| 6 | Security extensions (2FA, encryption at rest) | ⬜ |
| 7 | Hardening | ⬜ |

### Phase 0 — done
- Django 5.1 skeleton; 8 apps scaffolded (accounts, core, config, files, notifications, audit, public).
- `AUTH_USER_MODEL = accounts.User` set before first migration.
- Infra verified running: web/worker/beat + postgres/redis/minio/createbuckets; nginx conf; entrypoints; S3/MinIO storage backend; Redis cache + Celery broker; `/healthz`; Celery Beat schedule with `ping` + placeholder `purge_expired_files`.

### Phase 1 — what already exists (starter slice)
- `User` model with `employee_id`, `auth_source`, **temporary `role` string** (not real Role tables), profile fields, behaviour flags.
- `MultiIdentifierBackend` (local username/email/employee_id) — no LDAP, no throttle, no LoginAttempt.
- Basic login/logout views; dashboard shell with placeholder widgets.
- `SiteSettings` singleton (general + retention fields only).
- Only 2 migrations (accounts, config).

### Empty scaffolding (no models yet)
- `apps/audit/models.py`, `apps/files/models.py`, `apps/public/models.py` — stubs.
- `apps/notifications/tasks.py` — placeholders only.

---

## Remaining work

### Phase 1 — Foundation ✅ DONE (2026-06-11)
- [x] **Role / Permission / RolePermission tables** + `User.has_perm_code()`; `User.role` is now an FK; temp `role` string removed. Seeded SuperAdmin/Admin/Uploader (data migration `accounts/0003`).
- [x] **Audit models** (`apps/audit`): `LoginAttempt`, `ActivityLog`, `CronLog` (+ read-only admin).
- [x] **`get_client_ip()` helper** (`apps/core/utils.py`) keyed on `TRUSTED_PROXY_IPS`, XFF-spoof resistant.
- [x] **Login security flow** (§4): throttle + hard block in `apps/accounts/security.py`, wired into `MMLoginView`; LoginAttempt + ActivityLog on every login/logout; session rotation via Django's LoginView. Verified end-to-end.
- [x] **LDAP config + JIT provisioning** (§5): `MultiIdentifierBackend` LDAP bind + attribute sync + first-login JIT (role=Uploader) + local-wins collision; Fernet-encrypted bind password in SiteSettings.
- [x] SiteSettings: LDAP fields + write-only password admin field; `session_idle_timeout_minutes`.
- [ ] **PENDING (blocked):** rebuild the Docker image to install `ldap3` + `cryptography` so the LDAP runtime path and Fernet encryption execute. Corporate TLS-intercepting proxy is blocking pypi.org during `pip install`. Code is in place and imports lazily, so the app runs fine without them until the LDAP/encrypted-password paths are exercised.
- [ ] Session idle-timeout *enforcement* middleware (field exists; wiring is a small follow-up).

### Phase 2 — Files core ✅ DONE (2026-06-11) — verified against MinIO
- [x] Models: `StoredFile`, `UploadSession`, `FileAssignment`, `Category`; 4-state machine (uploading → pending_metadata → active → deleted).
- [x] Dual-path upload: direct (≤50 MiB, `files:upload`) + resumable chunked (`init_upload`→`upload_chunk`×N→`upload_complete`) onto MinIO multipart, with a vanilla-JS uploader that auto-picks the path.
- [x] Integrity: full-file SHA-256 (single-pass stream) + magic-byte content sniff (`apps/files/services.py`); disallowed-extension policy.
- [x] `activate_file()` — row-locked, idempotent: temp→final copy `files/{year}/{month}/{uuid}-{name}`, FileAssignment rows, enqueue tasks, delete temp + session.
- [x] Abandoned-upload purge (24h) in `apps/files/tasks.py` + CronLog; Beat now points here. Thumbnail Celery task (Pillow). my-uploads / my-files / detail pages, nav links, real dashboard widgets.
- [x] Basic authorized download via presigned MinIO URL (`files:download`).
- Deferred to **Phase 3**: full download-limit *reservation*, ZIP, public-link minting + email-verified public download (columns already on `StoredFile`). Content policy is a disallowed-extension blocklist for now (can tighten to an allowlist).

### Phase 3 — Download & public ✅ DONE (2026-06-11) — verified
- [x] Authorized download → presigned MinIO URL (original filename as download name); atomic row-locked `reserve_download_slot()` enforcing per-file `download_limit`; `DownloadEvent` recorded (audit §5.10).
- [x] ZIP download of selected files (`files:download_zip`) with per-file slot reservation; checkboxes on my-files list.
- [x] Public links: owner enable/rotate/disable on the detail page; `ensure_public_token`/`disable_public_link`; token minted on activation when public.
- [x] Email-verified public download (`apps/public`): `PublicDownloadVerification` (hashed 6-digit code, 15-min expiry, token snapshot), landing/request_code/verify_code/download views, anonymous session grant (file_id, email, token; ~1h TTL, no account), "verified-but-slot-taken" denied page, stale-link rejection.
- [x] `send_transactional_email` / `send_public_verification_code` (`apps/notifications/email.py`) — inline SMTP, never Celery.
- Note: stale-link rejection happens at the URL level (rotated token 404s) plus the `token_snapshot` check as defense-in-depth.

### Phase 4 — Notifications ✅ DONE (2026-06-11) — verified incl. real worker
- [x] Inline transactional (`send_transactional_email`/`send_public_verification_code`) now uses a runtime SMTP connection.
- [x] Deferred Celery path: `send_deferred_email` (retry+backoff, mark failed on giving up) + `enqueue_assignment_email`/`enqueue_welcome_email`/`enqueue_expiry_reminder`; `NotificationLog` enforces at-most-once via idempotency_key; `sent_at` set only on success; BCC from SiteSettings.
- [x] Assignment emails enqueued from `activate_file` (stamps `FileAssignment.notified_at`); welcome from LDAP JIT; `send_expiry_reminders` Beat task (daily) + schedule.
- [x] SMTP settings on SiteSettings (Fernet password, TLS/SSL, from, BCC, toggles) + admin UI; templates in `templates/email/`.
- Note: restart worker/beat after task-code changes (no Celery hot-reload). A real SMTP password needs the deferred `cryptography` install to encrypt; dev uses the console backend (no password).

### Phase 5 — Admin, groups, fields ✅ DONE (2026-06-11) — verified
- [x] Retention/expiry purge (§5.6.2 §8): `purge_expired_files` now runs step 1 retention (gated by `retention_enabled`, default OFF), step 2 per-file expiry (always), step 3 abandoned uploads, step 4 CronLog. `soft_delete_stored_file()` (object removed, row+audit kept).
- [x] Manual delete (`files:delete`, owner) + Delete button on detail page.
- [x] Groups (`accounts.Group` + admin); selected groups expand into per-member FileAssignments on activate.
- [x] Custom fields: admin-defined `config.CustomField` defs + `StoredFile.custom_fields` JSON; dynamic metadata form + display on detail.
- [x] Custom audit console (`/console/`): home + activity/logins/downloads, paginated, gated by `audit.view` (admins); nav link for admins.
- [x] Orphan import management command (`python manage.py import_orphans [--dry-run] [--owner X]`).
- Privacy: `is_hidden` flag exists on files; a fuller privacy/data-handling pass can come in Phase 7.

**v1 core (Phases 0–5) is complete.**

### Phase 6 — Security extensions
- [ ] Encryption at rest; TOTP + email 2FA (pending_2fa pre-auth marker, remember-me trusted-device); LDAP settings UI + connection test.

### Phase 7 — Hardening
- [ ] Security review, UAT, perf testing; production nginx + Gunicorn (replace `runserver`; non-root Celery worker).

---

## Sequencing notes
1. Do the **Role/Permission refactor + audit models first** — referenced by nearly every later flow; migrating `role` later (with real data) is painful.
2. **Phase 2 is the critical path** — Phases 3–5 all build on `StoredFile`.
