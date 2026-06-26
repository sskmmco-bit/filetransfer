# Change log — Replace MinIO with local-disk storage

**Plan:** `plans/20260626_090755_replace-minio-with-local-disk.md`
**Date:** 2026-06-26
**Status:** completed

## What changed

Removed the MinIO (S3) dependency entirely; file blobs now live on the app
host's local disk under `FILE_STORAGE_ROOT`. The storage abstraction in
`apps/files/storage.py` was rewritten for local disk, keeping the same public
function names/signatures everywhere except presigned URLs (which don't exist on
disk) — delivery is now done by `storage.serve()` (nginx `X-Accel-Redirect` in
prod, Django `FileResponse` in dev). The chunked/resumable upload flow is
preserved: the S3 multipart became an append-to-temp-file on disk.

**No database changes / no migrations.** `temp_key`/`storage_key`/
`thumbnail_key`/`multipart_upload_id`/`parts` are reused as-is (key strings are
now relative disk paths).

## Files changed

### Code
- `apps/files/storage.py` — rewritten: local-disk `put_object`, multipart-as-append
  (`create_multipart`/`upload_part(+offset)`/`complete_multipart`(no-op)/`abort_multipart`),
  `copy_object`, `delete_object`, `head_object`, `object_exists`, `get_object_body`;
  new `serve()` (X-Accel/FileResponse) + `_safe_path()` (blocks key escapes).
  Removed `get_client`/`get_presign_client`/`bucket`/`presigned_get_url`.
- `apps/files/views.py` — `download`/`preview`/`thumb` now `storage.serve(...)`
  instead of redirecting to a presigned URL; `detail.preview_url` →
  `reverse('files:preview', ...)`; `upload_chunk` passes `offset=received_size`
  for crash/resume-safe appends; dropped unused `HttpResponseRedirect` import;
  docstrings updated.
- `apps/public/views.py` — `preview`/`download` now `storage.serve(...)`;
  `download_all` unchanged (uses `get_object_body`); docstring updated.
- `apps/files/jobs.py` — docstring wording only (logic unchanged).
- `apps/files/services.py` — comment/docstring wording only.
- `apps/files/management/commands/import_orphans.py` — walks `FILE_STORAGE_ROOT`
  via `os.walk` instead of S3 `list_objects_v2`.
- `apps/core/views.py` — `healthz` now also checks the storage root is writable.
- `mmftp/settings.py` — removed `MINIO_*` + S3 `STORAGES["default"]`; added
  `FILE_STORAGE_ROOT`, `FILE_STORAGE_USE_X_ACCEL`, `FILE_STORAGE_X_ACCEL_PREFIX`,
  `MEDIA_ROOT`; `default` storage = `FileSystemStorage`.
- `requirements.txt` — removed `django-storages` and `boto3`.

### Deploy / infra
- `deploy/nginx.host.conf`, `deploy/nginx.compose.conf` — replaced the MinIO
  proxy with an `internal` `location /_protected/` aliasing the blob root.
- `deploy/install_no_docker.sh`, `deploy/wsl_test_install.sh` — removed MinIO
  binary/service/bucket steps; create + chown `FILE_STORAGE_ROOT`; `.env` and
  systemd units updated; removed `minio.service` from `After=` and status checks.
- `deploy/update_no_docker.sh` — idempotent cutover: ensures the file store,
  appends `FILE_STORAGE_*` to `.env` if missing, rewrites nginx with `/_protected/`,
  retires the legacy `minio.service`.
- `deploy/.env.production.template`, `.env.example` — `MINIO_*` → `FILE_STORAGE_*`.
- `docker-compose.dev.yml` — removed `minio` + `createbuckets` services (Postgres only).

### Docs
- `deploy/INSTALL_NO_DOCKER.md` — Step 5 rewritten (local dir, no MinIO), nginx
  `/_protected/`, `.env`, verify/backup/troubleshooting/HTTPS updated, plus a new
  "One-time data migration: MinIO → local disk" section.
- `CLAUDE.md`, `README.md`, `load_tests/README.md` — storage description updated.
- `ROADMAP.md` — left as-is (dated historical log; not rewritten).

## Verification
- `python manage.py check` — no issues.
- `py_compile` of all changed Python files — OK.
- Functional test of `storage.py` (temp root): path-escape blocked; put/exists/
  body; multipart append with offset-resume produces correct bytes
  (`AAAAACCCCC`); copy + delete; `serve()` dev `FileResponse` Content-Disposition
  (incl. non-ASCII `filename*`); `serve()` X-Accel header
  (`/_protected/files/2026/06/u-na%20me.pdf`) and Content-Type/Disposition. All pass.

## Deviations from plan
None. (ROADMAP.md was intentionally not rewritten — it is a dated history log
that already contains pre-Redis/Celery-removal entries.)

## Operational follow-ups (for the server)
- Run `deploy/update_no_docker.sh` on the existing host (creates the store,
  patches `.env`, rewrites nginx, retires MinIO unit).
- Perform the one-time MinIO→disk data copy (see INSTALL_NO_DOCKER.md) since the
  target host has existing files.
- Ensure disk headroom for the corpus (blobs now on the app host's disk; 5 GiB/file cap).
