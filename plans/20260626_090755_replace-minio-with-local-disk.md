# Replace MinIO object storage with local-disk storage

## Status
completed

## Decisions (approved 2026-06-26)
1. Existing MinIO data **must be migrated** — document a one-time bucket→disk copy
   in the update/install guide (preserve `files/…` and `thumbnails/…` key paths;
   drop `temp/…`).
2. Storage root: server default `FILE_STORAGE_ROOT=/var/lib/mmftp/files`, dev
   default `<repo>/media/files`. (User had no preference — using the proposed default.)
3. Scope includes deploy scripts + docs in this pass.

## Summary

Remove the MinIO (S3) dependency entirely and store file blobs on the local
filesystem of the app host. The decision is deliberate: this deployment will
never scale to S3, so an extra service to install, secure, and operate is pure
overhead. Management has confirmed it is not needed.

The whole storage surface is already isolated behind `apps/files/storage.py`
(a ~12-function boto3 wrapper). We rewrite that module to read/write a local
directory tree, keeping **the same function names and signatures** everywhere
except the one S3-only concept — **presigned URLs** — which cannot exist on
local disk. Today every presigned URL is consumed as a browser redirect
(`HttpResponseRedirect`/`redirect`) or as an `<img>/<iframe>/<video>` `src`.
We replace those with the app **streaming the bytes itself** through the
already-authorized `download`/`preview`/`thumb` views, using nginx
`X-Accel-Redirect` in production (efficient, zero-copy) and Django
`FileResponse` in dev. This is actually *more* secure: today a presigned URL is
a bearer token anyone can replay until it expires; after this change every byte
served passes through the existing permission / download-slot / audit logic.

The chunked/resumable upload flow is preserved end-to-end. The JS uploader and
its endpoints (`init_upload` → `upload_chunk`×N → `upload_complete`) are
**unchanged** at the API level. Under the hood, the S3 multipart upload becomes
an append-to-temp-file on disk: `create_multipart` creates the temp file,
`upload_part` appends a chunk, `complete_multipart` is a no-op (the file is
already assembled), `abort_multipart` deletes it. Parts still arrive strictly in
order (`next_part_number()`), and the existing duplicate-part guard in
`upload_chunk` keeps resumes idempotent.

**No database schema change.** `temp_key`, `storage_key`, `thumbnail_key`,
`multipart_upload_id`, and the `parts` JSON all keep working as-is — the key
strings simply become relative paths under the storage root instead of S3 keys.

### Storage layout on disk
Rooted at a new `FILE_STORAGE_ROOT` (env-configurable, e.g.
`/var/lib/mmftp/files`). Keys map 1:1 to relative paths, so the existing layout
is preserved:
- temp:      `temp/{uuid}/{filename}`
- final:     `files/{year}/{month}/{uuid}-{name}`
- thumbnail: `thumbnails/{uuid}.jpg`

A small `_safe_path(key)` joins key→root and refuses any path that escapes the
root (defends against `..`/absolute keys) before any read/write/delete.

## Files affected

### Core storage
- `apps/files/storage.py` — **changed (rewrite)**. Replace boto3 with local-disk
  ops. Keep: `put_object`, `create_multipart`, `upload_part`,
  `complete_multipart`, `abort_multipart`, `copy_object`, `delete_object`,
  `head_object`, `object_exists`, `get_object_body`. **Remove** `get_client`,
  `get_presign_client`, `bucket`, `presigned_get_url`. **Add** `serve(key, *,
  download_name, inline=False, content_type="")` returning a Django response
  (X-Accel-Redirect in prod, `FileResponse` in dev), and `_safe_path`.

### Views that served via presigned redirect → now stream
- `apps/files/views.py` — **changed**:
  - `download` — replace `HttpResponseRedirect(presigned_get_url(...))` with
    `return storage.serve(sf.storage_key, download_name=..., inline=False, content_type=...)`.
  - `preview` — stream inline instead of redirect.
  - `thumb` — stream the thumbnail (or original image) inline instead of redirect.
  - `detail` — set `preview_url = reverse("files:preview", uuid=sf.uuid)` (an
    authenticated streaming endpoint) instead of a presigned URL. The
    `data_missing` check keeps using `storage.object_exists`.
- `apps/public/views.py` — **changed**: `preview`, `download` stream instead of
  redirect; `download_all` is unchanged (already reads `get_object_body`).

### Jobs / commands
- `apps/files/jobs.py` — **changed**: no API change needed (uses `get_object_body`
  / `put_object` / `delete_object` / `abort_multipart`, all preserved). Verify
  thumbnail write path; update the module docstring wording (MinIO → local disk).
- `apps/files/management/commands/import_orphans.py` — **changed**: replace the
  S3 `list_objects_v2` paginator with an `os.walk` over `FILE_STORAGE_ROOT/files/`
  to find untracked blobs. Same behavior, filesystem source.
- `apps/files/services.py` — **changed (minor)**: comment/docstring wording only
  (logic already storage-agnostic). `complete_multipart` no-op still called — fine.

### Settings & deps
- `mmftp/settings.py` — **changed**: remove `MINIO_*` settings and the S3
  `STORAGES["default"]` block; set `default` to `FileSystemStorage`. Add
  `FILE_STORAGE_ROOT` (env, default `BASE_DIR/media/files`), `MEDIA_ROOT`,
  `FILE_STORAGE_USE_X_ACCEL` (bool, default `not DEBUG`), and
  `FILE_STORAGE_X_ACCEL_PREFIX` (default `/_protected/`). Keep WhiteNoise static.
- `requirements.txt` — **changed**: remove `django-storages` and `boto3` (nothing
  else imports them after the storage rewrite).
- `.env.example` / `deploy/.env.production.template` — **changed**: drop
  `MINIO_*`; add `FILE_STORAGE_ROOT` and the X-Accel toggle.

### Health probe
- `apps/core/views.py` — **changed (minor)**: `healthz` adds a cheap
  storage-root writability check alongside DB + cache.

### Deploy / infra
- `deploy/nginx.host.conf` & `deploy/nginx.compose.conf` — **changed**: remove the
  `/<bucket>/` MinIO proxy; add an `internal` `location /_protected/` aliasing
  `FILE_STORAGE_ROOT` for X-Accel-Redirect.
- `deploy/install_no_docker.sh`, `deploy/update_no_docker.sh`,
  `deploy/wsl_test_install.sh`, `deploy/INSTALL_NO_DOCKER.md` — **changed**:
  remove MinIO install/systemd unit/bucket bootstrap; create + chown
  `FILE_STORAGE_ROOT`.
- `docker-compose.dev.yml` — **changed**: remove the `minio` service (keep
  Postgres). Dev writes blobs under the local `media/` dir.

### Docs
- `CLAUDE.md`, `README.md`, `ROADMAP.md`, `load_tests/README.md` — **changed**:
  update references from "MinIO/presigned URLs" to "local disk / streamed via
  X-Accel". (Doc-only; can follow the code.)

## Database changes
**None.** No new/altered/removed models, fields, indexes, or migrations. The
`temp_key`/`storage_key`/`thumbnail_key`/`multipart_upload_id`/`parts` columns
are reused unchanged (key strings now denote relative disk paths).

## Effect on the current system

- **Behavioral / runtime**:
  - Downloads, previews, and thumbnails are now served by the Django app (via
    nginx `X-Accel-Redirect` in prod) instead of redirecting the browser to
    MinIO. No `/<bucket>/` URLs are ever handed out; presigned URLs are gone.
  - Chunked + direct uploads behave identically from the client's perspective.
  - The private-bucket + presigned-URL model is replaced by app-enforced authz on
    every byte (strictly tighter access control).
- **Backward compatibility / data migration**: existing blobs currently live in
  MinIO. Cutover requires a **one-time copy** of the bucket contents into
  `FILE_STORAGE_ROOT` preserving the key paths (`files/…`, `thumbnails/…`).
  `temp/…` objects (in-flight uploads) can be dropped — abandoned-upload cleanup
  handles the orphaned draft rows. This copy is an operational step, documented
  in the install/update guide; not a code migration. **If the target host has no
  existing data, this step is skipped.**
- **Services to restart / config**: restart `mmftp-web` after deploy.
  **Remove** the MinIO `systemd` unit and its data dir from the host. nginx must
  be reloaded with the new `internal` location. No Celery/worker involved.
  Background job timers pick up code changes automatically (no restart).
- **Config/secrets**: `MINIO_ACCESS_KEY`/`MINIO_SECRET_KEY` and all `MINIO_*`
  env vars are no longer used and can be deleted. New: `FILE_STORAGE_ROOT` (must
  be writable by the app user and, for X-Accel, readable by nginx),
  `FILE_STORAGE_USE_X_ACCEL`, `FILE_STORAGE_X_ACCEL_PREFIX`.
- **Capacity note**: blobs now consume the app host's local disk (5 GiB max per
  file). The 4 GB-RAM server's disk must be sized for the corpus; previously
  MinIO could be on a separate volume. Worth confirming disk headroom before
  cutover.
- **Rollback**: revert the code commit and restore the MinIO `systemd` unit +
  `.env` `MINIO_*` block; blobs still exist in MinIO (the cutover copy is
  additive, not a move, unless we choose to delete the bucket afterwards). Keep
  the MinIO data until the disk-based store is verified in production.

## Open questions for approval
1. **Existing data**: does the target host already hold files in MinIO that must
   be migrated, or is this effectively a fresh store (skip the copy step)?
2. **Storage root path**: default `FILE_STORAGE_ROOT=/var/lib/mmftp/files` on the
   server (dev → `<repo>/media/files`). OK, or a different mount?
3. **Scope of this change set**: do you want the deploy scripts + docs updated in
   the same pass, or code-only first (deploy/docs as a follow-up)?
