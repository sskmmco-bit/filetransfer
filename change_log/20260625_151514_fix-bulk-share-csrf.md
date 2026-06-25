# Change log — Fix bulk-share CSRF "incorrect length" / JSON.parse error

**Plan:** `plans/20260625_151514_fix-bulk-share-csrf.md`
**Date:** 2026-06-25

## What changed

`templates/files/my_uploads.html` — bulk-share submit handler (the `#bsSubmit`
click handler):

1. The `fetch` to `files:bulk_share` now reads the CSRF token live at send time
   (`var token = window.CSRF || csrf || ''`) instead of using the parse-time
   captured `csrf` local. The IIFE runs inside `{% block content %}` (base.html
   line 676) which is parsed **before** `window.CSRF` is assigned (base.html line
   974), so the old local was always `''`, producing an empty `X-CSRFToken`
   header and a 403 "incorrect length" from Django.

2. Added a response-status guard: if `!r.ok`, the handler re-enables the button
   and shows a readable message in `#bsErr` (a 403 → "Your session expired or the
   page is stale. Please refresh and try again."; other codes → "Share failed
   (HTTP n)."). This replaces the previous behavior of calling `r.json()` on the
   403 HTML error page, which threw the misleading
   `SyntaxError: JSON.parse: unexpected character at line 1 column 1`.

## Database changes

None.

## Behavior / deviations

- Bulk share and per-row "Share" from My Files now succeed.
- No deviations from the plan. The parse-time `var csrf = window.CSRF || ''`
  (line 185) was left in place as a harmless fallback; the fetch prefers the live
  `window.CSRF`.
- No migrations, no service restarts required (template is read per-render).
