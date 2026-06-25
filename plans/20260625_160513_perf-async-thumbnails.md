# Plan — Performance: async thumbnails via systemd timer

**Status:** completed

## Summary

Thumbnails are currently generated **synchronously on the request thread** during
`activate_file` (`services._enqueue_post_activation` → `jobs.generate_thumbnail`),
adding CPU + latency to image activations on the 2 vCPU box. This is the one real
latency cost of dropping Celery.

Move thumbnailing off the request path using the project's existing pattern
(management command on a systemd timer), so activation returns immediately and the
thumbnail is produced within ~1–2 minutes. This trades **immediate** thumbnails for
**eventually-consistent** ones (brief placeholder in the UI) — the standard async
tradeoff.

## Approach

- Stop calling `generate_thumbnail` inline. Image `StoredFile`s become "pending" =
  `status=ACTIVE`, `content_type LIKE image/%`, `thumbnail_key=''`, within size cap.
- New command `generate_pending_thumbnails` (timer ~1–2 min) batches pending images
  and generates each via the existing `generate_thumbnail`.
- Add `thumbnail_attempts` so permanently-failing images aren't retried forever; the
  job skips rows over a small cap (e.g. 3) and logs them.

## Files affected

- `apps/files/services.py` — **changed**: `_enqueue_post_activation` no longer calls
  `generate_thumbnail` (kept email enqueue). Docstring updated.
- `apps/files/jobs.py` — **changed**: add `generate_pending_thumbnails(limit=N)`
  (selects pending images, calls `generate_thumbnail`, writes a `CronLog`);
  `generate_thumbnail` increments `thumbnail_attempts` on failure and is skipped by
  the batch once over the cap.
- `apps/files/models.py` — **changed**: add
  `thumbnail_attempts = PositiveSmallIntegerField(default=0)`.
- `apps/files/management/commands/generate_pending_thumbnails.py` — **created**.
- `apps/files/migrations/0xxx_thumbnail_attempts.py` — **created**.
- `deploy/install_no_docker.sh`, `deploy/wsl_test_install.sh`,
  `deploy/update_no_docker.sh` — **changed**: add `mmftp-thumbnails.service` +
  `.timer` (~1–2 min, with the shared `OnFailure` logger), alongside the existing
  notifications/purge/reminders timers.
- `deploy/INSTALL_NO_DOCKER.md` — **changed**: document the new timer in the service
  table + day-to-day ops.
- `CLAUDE.md` — **changed**: update the "thumbnails generated synchronously" gotcha
  and the "Background jobs" section to reflect the new timer.

## Database changes

One new column `files_stored_file.thumbnail_attempts` (`smallint NOT NULL DEFAULT
0`) + migration. Additive, backward-compatible. No data migration (default 0).

## Effect on the current system

- **Runtime:** image activation returns faster (no inline thumbnail). Thumbnails
  appear within one timer interval (~1–2 min). Non-image activations unchanged.
- **UI:** templates must tolerate a temporarily-missing thumbnail (placeholder).
  Verify the file list / detail templates already fall back when `thumbnail_key` is
  empty (they do for non-image files); confirm during implementation.
- **Services:** `mmftp-web` restart (services.py change); install the new timer
  (installer/update script, or add the units manually + `daemon-reload`). Job
  timers re-exec code, so no restart needed for `jobs.py` edits after install.
- **Rollback:** revert code + remove the timer; the `thumbnail_attempts` column can
  remain (harmless) or be dropped via reverse migration. Re-enabling inline
  generation is a one-line revert in `_enqueue_post_activation`.

## Open question for approval

This makes thumbnails appear with a short delay instead of instantly. Confirm that
~1–2 min eventual-consistency is acceptable for the file-transfer UX. If instant
thumbnails are required, we keep it inline and instead just cap source size more
aggressively (already 25 MiB).
