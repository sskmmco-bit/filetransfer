# Change log — Remove Redis (local cache) + dev Docker compose

**Plan:** `plans/20260625_140041_remove-redis-use-local-cache.md`
**Date:** 2026-06-25

## What was changed

Removed Redis from the stack entirely (the Celery removal had already dropped its
broker role; it remained only as the Django cache backend) and added a dev-only
Docker compose for Postgres + MinIO.

### Cache backend
- `mmftp/settings.py` — replaced the `REDIS_URL` + `django_redis.cache.RedisCache`
  `CACHES` block with `django.core.cache.backends.locmem.LocMemCache`
  (LOCATION `mmftp-default`). No `REDIS_URL` env read anymore.
- `apps/core/views.py` — `healthz()` now reports a `cache` check instead of
  `redis` (key + docstring); the cache round-trip logic is unchanged.

### Dependencies / env
- `requirements.txt` — removed `redis==5.2.1` and `django-redis==5.4.0`.
- `.env.example`, `deploy/.env.production.template` — removed the `REDIS_URL` /
  `REDIS_HOST` / `REDIS_PORT` block (and leftover `CELERY_*` vars where present).

### Deploy scripts / docs
- `deploy/install_no_docker.sh`, `deploy/wsl_test_install.sh` — dropped
  `redis-server` from apt install + `systemctl enable --now`; removed
  `REDIS_*`/`CELERY_*` from the generated `.env`; removed redis from the status
  line and from systemd `After=`.
- `deploy/update_no_docker.sh`, `deploy/INSTALL_NO_DOCKER.md` — removed redis from
  systemd `After=` lines; install doc service table, STEP 1 package install, env
  template, status check, and health-probe note de-Redis'd.
- `load_tests/README.md` — dropped a stale "Redis" mention from the
  "what hitting Gunicorn isolates" note.
- `README.md`, `CLAUDE.md` — stack/intro, dev prerequisites, `/healthz` description
  (DB + cache), feature list; README's stale `mmftp-worker`/`mmftp-beat` production
  line corrected to `mmftp-web` + the three job timers.

### New dev tooling
- `docker-compose.dev.yml` — **created**. Dev-only stack: PostgreSQL
  (`postgres:16-alpine`, host port **55432** → container 5432) + MinIO (:9000 S3,
  :9001 console) + a one-shot `minio/mc` bucket-creator for `mmftp-files`. No
  Redis, no app/worker/beat containers — Django runs on the host via `runserver`.
  Postgres is published on the high port 55432 (not 5432) because this dev machine
  has native Windows PostgreSQL clusters already holding 5432 and 5433; the
  matching `POSTGRES_PORT=55432` goes in `.env`.

## Database
None. LocMemCache is in-memory; no cache table, no model/migration changes.

## Verification
- `python manage.py check` → "System check identified no issues (0 silenced)."
- Grep confirms no remaining functional references to `redis` / `django_redis` /
  `REDIS_URL` in code — only historical comments/docstrings remain.
- End-to-end: `docker compose -f docker-compose.dev.yml up -d` → `migrate` applied
  (`notifications.0002` + existing) against the container Postgres → `runserver` +
  `GET /healthz` returned `200 {"status":"ok","database":"ok","cache":"ok"}`
  (no Redis in the loop).

## Deviations from the plan
- Also removed leftover `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` lines found
  in the deploy install scripts and INSTALL_NO_DOCKER.md (the prior Celery-removal
  change had missed these in the no-Docker installers).
