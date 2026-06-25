# Replace Celery with management commands + systemd timers

**Status:** completed

## Summary

Remove the Celery + Redis-broker background-task layer and re-implement the four
existing background jobs using the pattern already specified in
`MIGRATION_PLAN.md`: Django **management commands driven by systemd timers**, with
the existing `NotificationLog` table acting as the deferred-email queue, plus one
job moved to **synchronous in-request** execution.

Why: Celery + a Redis broker + a long-running worker and beat process are heavy
machinery for four low-volume jobs in an internal app. A DB-backed queue drained
by a timer removes the broker, the two always-on `systemd` units, and the
`celery` dependency, while keeping at-most-once delivery and retry semantics.

**Scope is the four current tasks only.** This is *not* the full
`MIGRATION_PLAN.md` re-platform — MinIO object storage, the `NotificationLog`
model, and `apps/files/storage.py` are all kept as-is. Redis is **kept** as the
Django cache backend (`CACHES`); only its use as a Celery broker is removed.

### Mapping (current → new)

| Current Celery task | New mechanism |
|---|---|
| `send_deferred_email` + `enqueue_*` helpers | `send_queued_notifications` management command, systemd timer every ~2 min; **claims** `NotificationLog` PENDING rows (marks `status=SENDING`, `claimed_at=now`) under `select_for_update(skip_locked=True)` in a short transaction, then sends SMTP **outside** the lock; per-row retry until `MAX_RETRIES`, then `failed` |
| `generate_thumbnail` | Called **synchronously** in `services._enqueue_post_activation` (runs in `transaction.on_commit`); already best-effort/try-excepted |
| `purge_expired_files` (beat, daily) | `purge_expired_files` management command, daily systemd timer; writes `CronLog` unchanged |
| `send_expiry_reminders` (beat, daily) | `send_expiry_reminders` management command, daily systemd timer |

Decisions confirmed earlier: **systemd timers** (consistent with the existing
`mmftp-web`/`-worker`/`-beat` units) and **`select_for_update(skip_locked=True)`**
for overlap-safe draining.

## Files affected

**Created**
- `plans/20260617_103718_replace-celery-with-cron-commands.md` *(this plan)*
- `apps/notifications/management/__init__.py`
- `apps/notifications/management/commands/__init__.py`
- `apps/notifications/management/commands/send_queued_notifications.py`
- `apps/notifications/management/commands/send_expiry_reminders.py`
- `apps/files/management/commands/purge_expired_files.py`
- `apps/notifications/migrations/0002_notificationlog_claim.py` *(add `SENDING` choice + `claimed_at` — see Database changes; exact number depends on the latest migration)*

**Renamed** (cleaner naming now that these hold plain functions, not Celery tasks)

- `apps/notifications/tasks.py` → `apps/notifications/jobs.py` — *and* drop
  `@shared_task`/`.delay()`; `send_deferred_email` becomes a plain per-row send
  function; add a `drain_pending()` helper with a **two-phase claim/send**:
  (1) in a short `transaction.atomic()`, `select_for_update(skip_locked=True)` a
  batch of claimable rows (`PENDING`, or `SENDING` whose `claimed_at` is older than
  a stale bound), mark them `status=SENDING`, `claimed_at=now`, commit (lock
  released); (2) send SMTP **outside** the lock, then set `SENT`/`sent_at` on
  success, or increment `attempts` and set back to `PENDING` (retry next tick) /
  `FAILED` once `attempts >= MAX_RETRIES`. `send_expiry_reminders` becomes a plain
  function; `_enqueue` no longer calls `.delay()` (just leaves the row PENDING) **and
  resets only `FAILED`/`PENDING` rows — never a `SENDING` row** (Risk: mid-send reset);
  the drain writes one `CronLog` row per run for observability; stale bound is
  `NOTIFICATION_CLAIM_STALE_MINUTES` (default 15); remove the `ping` task.
- `apps/files/tasks.py` → `apps/files/jobs.py` — *and* drop `@shared_task`;
  `generate_thumbnail` and `purge_expired_files` become plain importable functions.
  `generate_thumbnail` **skips** sources larger than `THUMBNAIL_MAX_SOURCE_BYTES`
  (default 25 MiB) so a huge image can't stall the request thread (Risk: synchronous
  thumbnailing).

**Changed** (import sites for the rename + the inline-thumbnail switch)

- `apps/files/services.py` — update `from apps.notifications.tasks import …` →
  `apps.notifications.jobs` (2 sites) and `from .tasks import generate_thumbnail` →
  `from .jobs import generate_thumbnail`; `_enqueue_post_activation` calls
  `generate_thumbnail(pk)` synchronously instead of `.delay()`.
- `apps/accounts/ldap_provision.py` — `from apps.notifications.tasks import enqueue_welcome_email`
  → `apps.notifications.jobs`.
- `apps/notifications/email.py` — fix the docstring reference (`apps.notifications.tasks`
  → `apps.notifications.jobs`; drop "handed to Celery"); add an explicit SMTP socket
  **timeout** (~30 s) on the connection so a send can't outlive the claim stale bound
  (Risk: double-send window).
- `apps/notifications/models.py` — add `SENDING = "sending"` to `NotificationStatus`
  and a `claimed_at = models.DateTimeField(null=True, blank=True)` field on
  `NotificationLog` to support claim-marking + stale-claim reclaim (Risk 1).
- `mmftp/__init__.py` — remove the `celery_app` import (whole file becomes empty/removed marker).
- `mmftp/settings.py` — delete the `CELERY_*` settings block; keep `REDIS_URL`/`CACHES`.
- `requirements.txt` — remove `celery==5.4.0`; keep `redis` + `django-redis` (cache).
- `deploy/INSTALL_NO_DOCKER.md` — replace the `mmftp-worker`/`mmftp-beat` service
  sections with `.timer` + oneshot `.service` units for the three commands (each
  service with an `OnFailure=` journald log); update the service-list, troubleshooting,
  and restart sections; document `systemctl list-timers` + `journalctl -u
  mmftp-notifications` as the timer health check (Risk: silent timer failures).
- `deploy/install_no_docker.sh` — replace worker/beat unit heredocs with timer units.
- `deploy/wsl_test_install.sh` — same.
- `deploy/update_no_docker.sh` — restart timers; drop worker/beat restart.
- `deploy/.env.production.template` — drop Celery broker/result vars (keep `REDIS_URL`).
- `CLAUDE.md` — rewrite the **Celery** architecture section, the dev/prod command
  blocks (no `celery worker`/`beat`; new timers + `manage.py` commands), the
  "verify Celery" block, and the worker/beat restart gotcha; add a gotcha noting
  thumbnails are now generated synchronously on activation (latency cost +
  `THUMBNAIL_MAX_SOURCE_BYTES` cap).
- `README.md` — update the Celery/ping verification snippet.

**Deleted**
- `mmftp/celery.py` (includes `debug_task`).

## Database changes

**One migration** (`notifications`), to support claim-marking of in-flight sends
(Risk 1):

- `NotificationStatus` gains a `SENDING = "sending"` choice (claimed, send in
  progress). Changing `choices` is a no-op `AlterField` at the DB level but Django
  still records it in the migration.
- `NotificationLog` gains `claimed_at = DateTimeField(null=True, blank=True)` —
  set when a row is claimed; used to reclaim rows stuck in `SENDING` after a
  crashed run (older than a stale bound).

Generated with `makemigrations` and **committed** (per the CLAUDE.md gotcha). No
data migration — existing `PENDING`/`SENT`/`FAILED` rows remain valid; `claimed_at`
defaults to `NULL`. The systemd unit files are generated by the install scripts and
are not committed source.

## Effect on the current system

**Behavioral**
- **Deferred email** (assignment / welcome / expiry-reminder) is sent on the next
  timer tick (~up to 2 min) instead of seconds after commit. Acceptable — these
  are explicitly "fire-and-forget" (§5.11). At-most-once is preserved via the
  existing unique `idempotency_key`; retries re-run each tick until `MAX_RETRIES`,
  then `failed` (same terminal behavior as today, fixed-interval instead of
  Celery backoff).
- **Transactional email** (public verification codes, etc.) is unchanged — still
  inline, never queued.
- **Thumbnails** are generated synchronously after activation commits, adding a
  small latency (Pillow, 320px) to image activations; sources over
  `THUMBNAIL_MAX_SOURCE_BYTES` (25 MiB) are skipped. Still best-effort: a failure
  is logged and never blocks activation.
- **Purge / expiry-reminder** behavior and `CronLog` output are unchanged; only the
  trigger (systemd timer vs. Celery beat) changes.

**Operational / deployment**
- Removes the always-on `mmftp-worker` and `mmftp-beat` units and the Celery broker.
  Adds three timer+oneshot-service pairs: `mmftp-notifications.timer` (~2 min),
  `mmftp-purge.timer` (daily 03:00), `mmftp-reminders.timer` (daily 07:00).
- **On existing servers:** `systemctl disable --now mmftp-worker mmftp-beat`, remove
  their unit files, install the new timers (`update_no_docker.sh` will handle this).
- `pip install -r requirements.txt` after the dependency change.
- **Redis is still required** (Django cache); `/healthz` Redis probe stays.
- The "restart worker/beat after task-code changes" gotcha is replaced by "timers
  re-exec `manage.py` each run, so code changes are picked up automatically."

**Backward-compatibility / rollback**
- No data migration; `NotificationLog` rows from the Celery era remain valid and
  will be drained by the new command. Rollback = revert the commit, reinstall
  `celery`, and re-enable the `mmftp-worker`/`mmftp-beat` units.

**Risks**

- **Row lock during SMTP send (mitigated by claim-marking).** SMTP can be slow, so
  the drain never holds a row lock across a send. Instead it **claims** rows in a
  short transaction — `select_for_update(skip_locked=True)`, set `status=SENDING` +
  `claimed_at=now`, commit (lock released) — then sends outside the lock. Overlapping
  timer runs claim *different* rows (no double-send, no blocking).
- **Double-send window if the stale bound is shorter than a real send (correctness).**
  Re-claiming `SENDING` rows older than the stale bound recovers crashed runs, but if
  a *genuine* send merely runs slow (longer than the bound), a second run re-claims
  and **both send the same email**. The unique `idempotency_key` does **not** prevent
  this — it dedupes *rows*, not repeated SMTP sends of one row.
  **Mitigation:** give SMTP an explicit socket **timeout** (e.g. 30 s) and set the
  stale bound well above it (e.g. **15 min**, `NOTIFICATION_CLAIM_STALE_MINUTES`), so
  a send can never outlive its own timeout — re-claim then only ever hits dead runs.
- **`_enqueue` resetting a mid-send row (correctness).** The current `_enqueue` resets
  any non-`SENT` row to `PENDING`; if it fires while the drain holds a row in
  `SENDING`, that row gets re-queued and re-sent.
  **Mitigation:** `_enqueue` resets only `FAILED` (and already-`PENDING`) rows — never
  a `SENDING` row.
- **Synchronous thumbnailing ties up a Gunicorn worker.** Inline generation now runs
  MinIO download → Pillow decode → MinIO upload on the request thread (the Celery
  worker previously absorbed this off-request); a large image blocks one worker for
  its duration.
  **Mitigation:** keep it best-effort (failure never blocks activation) and **skip**
  thumbnailing above a size cap (`THUMBNAIL_MAX_SOURCE_BYTES`, e.g. 25 MiB) — large
  originals get no thumbnail rather than stalling the request. Documented as a known
  latency cost in `CLAUDE.md`.
- **No always-on process → silent timer failures.** With Celery gone, a misconfigured
  oneshot unit (venv/`.env` not loaded) "runs" but errors with nothing watching.
  **Mitigation:** `send_queued_notifications` writes a `CronLog` row per run (like
  `purge_expired_files`); add `OnFailure=` journald logging to each unit; document
  `systemctl list-timers` + `journalctl -u mmftp-notifications` as the health check in
  `INSTALL_NO_DOCKER.md`.
- **`tasks.py` → `jobs.py` rename (mechanical).** Touches three import sites
  (`apps/files/services.py` ×3, `apps/accounts/ldap_provision.py` ×1) plus a docstring
  in `apps/notifications/email.py` — all enumerated above. A missed import raises
  `ImportError` at the first call site, so each must be updated in the same change.
