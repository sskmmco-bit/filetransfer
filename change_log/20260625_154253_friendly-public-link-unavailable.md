# Change log — Friendly "link unavailable / expired" page for public share links

**Plan:** `plans/20260625_154253_friendly-public-link-unavailable.md`
**Date:** 2026-06-25

## What changed

Previously, a disabled/expired/empty public share link raised a bare
`Http404("This link is invalid or has expired.")`, which rendered Django's raw
yellow debug 404 page (DEBUG) or the generic 404 (production). External
recipients now get a clean, branded page instead.

### `apps/public/views.py` (changed)
- Removed the `from django.http import Http404` import (no longer used directly).
- Added `import` of `functools.wraps`.
- Added exception `LinkUnavailable(Exception)` for missing/disabled/expired/empty
  links.
- Added decorator `public_link_view(view)` that catches `LinkUnavailable` and
  renders `public/unavailable.html` with `status=404` (works in DEBUG too, since
  a rendered response bypasses the debug 404 page).
- `_link()` and `_link_files()` now `raise LinkUnavailable(...)` instead of
  `Http404(...)`.
- Decorated all seven public views with `@public_link_view` (placed under the
  existing `@require_http_methods`): `landing`, `submit_password`,
  `request_code`, `verify_code`, `preview`, `download`, `download_all`.

### `templates/public/unavailable.html` (created)
- Extends `base.html`; centered card with a warning icon and a clear message:
  "This link is no longer available — it may have expired, been disabled by the
  sender, or the files were removed. Please ask the sender to share the files
  with you again."

## Status code

404 Not Found (per user's choice; 410 Gone was offered as an alternative).

## Database changes

None.

## Behavior / deviations

- No deviations from the plan.
- As noted in the plan, `_link_file()` (a missing *individual* file uuid under an
  otherwise-valid link) still raises `Http404` via `get_object_or_404` — left
  out of scope; can be folded into the same page later if wanted.
- No migrations, no service restarts required. Verified `apps/public/views.py`
  parses (`ast.parse`) and contains no remaining `Http404` references.
