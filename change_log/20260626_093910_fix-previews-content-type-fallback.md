# Change log — Fix file previews (content-type extension fallback)

**Date:** 2026-06-26
**Status:** completed
**Related:** follow-up to `change_log/20260626_092836_replace-minio-with-local-disk.md`
(user reported PDF/image/etc. previews not showing). No prior plan file — small
bugfix done at the user's explicit request.

## Root cause
`libmagic` is unavailable on the Windows dev box, so `sniff_content_type()`
returns `application/octet-stream` for every upload. `preview_kind()` keyed off
content_type only, so it returned "none" for all files → no preview element
rendered, and delivery used a generic type so browsers downloaded instead of
rendering. (Independent of the MinIO→disk change.)

## Fix
Fall back to the **filename extension** when content_type is missing/generic.

- `apps/files/views.py` — `preview_kind()` now falls back to an extension map
  (`_PREVIEW_EXT`: image/pdf/video/audio/text) when content_type is empty or
  `application/octet-stream`. Fixes the detail-page preview, the grid preview
  icon, and the `thumb` fallback (which serves the original image inline).
- `apps/files/storage.py` — new `_effective_content_type()`; `serve()` now sets
  `Content-Type` from the filename when the stored type is generic, so inline
  previews render (both the FileResponse and X-Accel paths).
- `apps/files/services.py` — new `_resolve_content_type()`; `complete_transfer()`
  derives the type from the extension (via `mimetypes`) when sniffing yields
  octet-stream, so **new** uploads store a correct content_type.
- `apps/files/management/commands/fix_content_types.py` — new idempotent backfill
  command: re-derives content_type for existing octet-stream/empty rows (so image
  thumbnails regenerate and stats are correct). Supports `--dry-run`.

## Notes / limitations
- **Word/Excel/PowerPoint (.doc/.docx/.xls/.xlsx/.ppt/.pptx) cannot be previewed
  inline by browsers** — they require conversion or an external Office/Google
  viewer (not available on an air-gapped host). These continue to show "no inline
  preview" + Download. Supported inline: images, PDF, text/code, audio, video.
- Existing files preview immediately via the render-time fallback (no re-upload).
  Run `python manage.py fix_content_types` to also regenerate real image
  thumbnails for previously-uploaded images.

## Follow-up fix — iframe previews blocked by X-Frame-Options
After the content-type fix, PDF/text previews still failed in the detail page
with Chrome's "127.0.0.1 refused to connect." inside the `<iframe>`. Cause:
serving the file through Django (instead of the old redirect to MinIO) means
Django's `XFrameOptionsMiddleware` stamps `X-Frame-Options: DENY`, which blocks
the same-origin iframe. Fix: `storage.serve()` now sets
`X-Frame-Options: SAMEORIGIN` on the response (Django's middleware respects an
already-set header), so our own pages can embed inline previews. Applies to both
the FileResponse (dev) and X-Accel (prod) paths and to public previews.
(Images/audio/video load via `<img>/<video>/<audio>` and were never affected.)

## Verification
- `manage.py check` clean; changed files byte-compile.
- `preview_kind('application/octet-stream', …)` → pdf/image/text/video correctly;
  `.docx` → none; `_effective_content_type` → application/pdf, image/png.
- `serve(... inline pdf)` → Content-Type application/pdf, Content-Disposition
  inline, `X-Frame-Options: SAMEORIGIN`.
