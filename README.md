# MMFileTransfer

Internal file-transfer application — **Phase 0 / Phase 1 starter scaffold**.

Stack: **Django 5 + Gunicorn**, **nginx** reverse proxy, **PostgreSQL**, **MinIO**
(S3-compatible object storage), **Redis** + **Celery** (workers & Beat). Everything
runs in Docker.

---

## Quick start (development)

```bash
cp .env.example .env          # then edit secrets if you like
docker compose up --build
```

That brings up: `postgres`, `redis`, `minio` (+ a one-shot bucket creator),
`web` (Django autoreload), `worker`, and `beat`.

On first boot the `web` container waits for Postgres, generates migrations,
applies them, collects static, and (optionally) creates a superuser from the
`DJANGO_SUPERUSER_*` env values.

Once it's up:

| URL | What |
|-----|------|
| http://localhost:8000/ | Dashboard (redirects to login) |
| http://localhost:8000/accounts/login/ | Login — accepts employee ID, email, **or** username |
| http://localhost:8000/admin/ | Django admin |
| http://localhost:8000/healthz | JSON health probe (checks DB + Redis) |
| http://localhost:9001/ | MinIO console (`minioadmin` / `minioadmin`) |

Default superuser (from `.env.example`): **admin / adminpass123**.

## Production-like run

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up --build -d
```

This runs the web app under **Gunicorn** behind **nginx** (port 80), with no code
bind-mount and `DEBUG=False`. Set real secrets, `DJANGO_DEBUG=False`,
`DJANGO_SECURE_SSL=True`, and a proper `TRUSTED_PROXY_IPS` / `DJANGO_ALLOWED_HOSTS`
in `.env` first.

## Useful commands

```bash
docker compose exec web python manage.py createsuperuser
docker compose exec web python manage.py makemigrations
docker compose exec web python manage.py migrate
docker compose exec worker celery -A mmftp inspect ping
docker compose logs -f web worker beat
```

Verify Celery end-to-end:

```bash
docker compose exec web python manage.py shell -c \
  "from apps.notifications.tasks import ping; print(ping.delay().get(timeout=10))"
# -> pong
```

---

## Project layout

```
mmftp/                 Django project package (settings, urls, wsgi, asgi, celery)
apps/
  accounts/            Custom User model + multi-identifier login backend (§5.7.1)
  core/                Dashboard shell (§5.16) + /healthz
  config/              SiteSettings singleton (§13.3)
  files/               StoredFile / uploads / downloads      (Phase 2 — stub)
  notifications/       Celery tasks (deferred email, purge)  (sample tasks wired)
  audit/               ActivityLog / LoginAttempt / etc.     (Phase 1 — stub)
  public/              Public links + secure download        (Phase 3 — stub)
templates/             base, login, dashboard
docker/                entrypoints + nginx config
docker-compose.yml     dev stack
docker-compose.prod.yml  prod override (gunicorn + nginx)
```

## What's included in this starter

- Custom `accounts.User` (`AUTH_USER_MODEL` set before first migration — §3) with
  `employee_id`, `auth_source`, a simple `role`, profile fields, and behaviour flags.
- **Multi-identifier login** — resolves employee ID / email / username, local-wins (§5.7.1a).
- Dashboard shell with placeholder widgets, login-gated.
- `SiteSettings` singleton with general + retention fields.
- Settings fully wired for PostgreSQL, Redis cache, Celery (broker/result), and
  MinIO object storage (presigned URLs, private bucket).
- `get_client_ip()`-ready proxy settings (`TRUSTED_PROXY_IPS`, `SECURE_PROXY_SSL_HEADER`).
- Sample Celery task (`ping`) + scheduled `purge_expired_files` placeholder (Beat).
- Health endpoint that checks DB and Redis.

## What's intentionally deferred (next steps from the plan)

- Full **Role / Permission / RolePermission** tables and the permission matrix (§2.4, §5.7.4).
- **LDAP** auth + JIT provisioning (§5.7), 2FA (§5.17), password reset + throttling (§5.9).
- Audit models + `get_client_ip()` helper (§5.14, §5.15, §3).
- `files` app: two-step upload to MinIO, `StoredFile`/`UploadSession`/`FileAssignment` (§5.5, §5.8).
- `public` app: token links + email-verified download (§5.4).
- Deferred email delivery + templates (Phase 4) and the real retention/expiry purge (§5.6.2).

## Notes

- Migrations are generated at container boot for convenience. For real work, run
  `makemigrations` locally and **commit the migration files** to the repo.
- `SECRETS_ENCRYPTION_KEY` is empty by default. Generate one before wiring SMTP/LDAP:
  `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`
- Static files are served by WhiteNoise (and nginx in prod). File blobs go to MinIO;
  downloads will use short-lived presigned URLs (Phase 3).
