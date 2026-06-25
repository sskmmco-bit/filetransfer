# MMFileTransfer

Internal file-transfer application — **Phase 0 / Phase 1 starter scaffold**.

Stack: **Django 5 + Gunicorn**, **nginx** reverse proxy, **PostgreSQL**, **MinIO**
(S3-compatible object storage). The Django cache is in-process (no Redis).
Background jobs run as Django management commands on **systemd timers** (no Celery).
Runs directly on a Linux host — **no Docker**.

---

## Quick start (development)

You need PostgreSQL and MinIO running first (no Redis). For local dev on
Windows/macOS the easiest way is the dev compose file:

```bash
docker compose -f docker-compose.dev.yml up -d   # Postgres + MinIO + bucket
```

For a server, install them as host services — see
[`deploy/INSTALL_NO_DOCKER.md`](deploy/INSTALL_NO_DOCKER.md). Then:

```bash
python -m venv venv
venv/bin/pip install -r requirements.txt
cp .env.example .env              # then edit hosts/secrets to taste
set -a; source .env; set +a       # load env into the shell
python manage.py migrate
python manage.py collectstatic --noinput
python manage.py createsuperuser
python manage.py runserver        # Django autoreload
```

Background jobs are plain management commands — run them on demand in dev (in
production they run on systemd timers):

```bash
python manage.py send_queued_notifications   # drain deferred-email queue
python manage.py purge_expired_files         # retention/expiry/abandoned cleanup
python manage.py send_expiry_reminders       # queue expiry reminders
```

Once it's up:

| URL | What |
|-----|------|
| http://localhost:8000/ | Dashboard (redirects to login) |
| http://localhost:8000/accounts/login/ | Login — accepts employee ID, email, **or** username |
| http://localhost:8000/admin/ | Django admin |
| http://localhost:8000/healthz | JSON health probe (checks DB + cache) |
| http://localhost:9001/ | MinIO console |

## Production-like run

On a server, the app runs under **Gunicorn** behind **nginx** (port 80) with
`DEBUG=False` as the `mmftp-web` `systemd` unit, with background jobs on three
`systemd` timers (`mmftp-notifications`, `mmftp-purge`, `mmftp-reminders`). The
full procedure — plus a one-command installer
(`deploy/install_no_docker.sh`) — is in
[`deploy/INSTALL_NO_DOCKER.md`](deploy/INSTALL_NO_DOCKER.md). Set real secrets,
`DJANGO_DEBUG=False`, `DJANGO_SECURE_SSL=True`, and a proper `TRUSTED_PROXY_IPS` /
`DJANGO_ALLOWED_HOSTS` in `.env` first.

## Useful commands

With the venv active and `.env` loaded:

```bash
python manage.py createsuperuser
python manage.py makemigrations
python manage.py migrate
python manage.py send_queued_notifications   # drain deferred-email queue on demand
```

On a server install, use systemd for the web service + job timers:

```bash
sudo systemctl restart mmftp-web                 # job timers re-exec code each run
journalctl -u mmftp-web -f
systemctl list-timers 'mmftp-*' --no-pager       # next/last run of each job
```

Run a background job immediately (instead of waiting for its timer):

```bash
sudo systemctl start mmftp-notifications.service   # or mmftp-purge / mmftp-reminders
journalctl -u mmftp-notifications -n 50
```

---

## Project layout

```
mmftp/                 Django project package (settings, urls, wsgi, asgi)
apps/
  accounts/            Custom User model + multi-identifier login backend (§5.7.1)
  core/                Dashboard shell (§5.16) + /healthz
  config/              SiteSettings singleton (§13.3)
  files/               StoredFile / uploads / downloads      (Phase 2 — stub)
  notifications/       Deferred email queue + drain (jobs.py) (§5.11)
  audit/               ActivityLog / LoginAttempt / etc.     (Phase 1 — stub)
  public/              Public links + secure download        (Phase 3 — stub)
templates/             base, login, dashboard
deploy/                no-Docker install guide, installer scripts, nginx + env templates
```

## What's included in this starter

- Custom `accounts.User` (`AUTH_USER_MODEL` set before first migration — §3) with
  `employee_id`, `auth_source`, a simple `role`, profile fields, and behaviour flags.
- **Multi-identifier login** — resolves employee ID / email / username, local-wins (§5.7.1a).
- Dashboard shell with placeholder widgets, login-gated.
- `SiteSettings` singleton with general + retention fields.
- Settings fully wired for PostgreSQL, in-process cache, and MinIO object storage
  (presigned URLs, private bucket).
- `get_client_ip()`-ready proxy settings (`TRUSTED_PROXY_IPS`, `SECURE_PROXY_SSL_HEADER`).
- Background jobs as management commands on systemd timers (`send_queued_notifications`,
  `purge_expired_files`, `send_expiry_reminders`) — no Celery/broker.
- Health endpoint that checks DB and cache.

## What's intentionally deferred (next steps from the plan)

- Full **Role / Permission / RolePermission** tables and the permission matrix (§2.4, §5.7.4).
- **LDAP** auth + JIT provisioning (§5.7), 2FA (§5.17), password reset + throttling (§5.9).
- Audit models + `get_client_ip()` helper (§5.14, §5.15, §3).
- `files` app: two-step upload to MinIO, `StoredFile`/`UploadSession`/`FileAssignment` (§5.5, §5.8).
- `public` app: token links + email-verified download (§5.4).
- Deferred email delivery + templates (Phase 4) and the real retention/expiry purge (§5.6.2).

## Notes

- Run `makemigrations` locally and **commit the migration files** to the repo.
  A server install applies committed migrations only — it does not generate them.
- `SECRETS_ENCRYPTION_KEY` is empty by default. Generate one before wiring SMTP/LDAP:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- Static files are served by WhiteNoise (and nginx in prod). File blobs go to MinIO;
  downloads will use short-lived presigned URLs (Phase 3).
