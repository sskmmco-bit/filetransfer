# Fix bulk-share CSRF "incorrect length" / JSON.parse error

**Status:** completed

## Summary

Sharing files from **My Files** (`/files/mine/`) fails. The server logs show
`Forbidden (CSRF token from the 'X-Csrftoken' HTTP header has incorrect length.): /files/bulk-share/`
and the share modal shows `Error: SyntaxError: JSON.parse: unexpected character at line 1 column 1 of the JSON data`.

Root cause: in `templates/files/my_uploads.html`, the bulk-share IIFE captures the
CSRF token into a local variable at **parse time**:

```js
var csrf = window.CSRF || '';   // line 185
```

But `window.CSRF` is assigned later in the document, in `templates/base.html`:

```js
window.CSRF = "{{ csrf_token }}";   // base.html line 974
```

The page layout renders `{% block content %}` (base.html line 676 — where the
my_uploads script lives) **before** the line-974 assignment. So when the IIFE runs,
`window.CSRF` is still `undefined` and `csrf` becomes `''`. The bulk-share `fetch`
then sends an empty `X-CSRFToken` header. Django sees a present-but-empty header
(length 0 ≠ 64) and rejects with 403 "incorrect length", returning an **HTML**
error page. The JS then calls `await r.json()` on that HTML, which throws the
misleading `JSON.parse` SyntaxError.

This is unrelated to the Redis→LocMemCache change — CSRF here is cookie-based
(no `CSRF_USE_SESSIONS`).

Two fixes:
1. **Send the live token** — read `window.CSRF` at fetch time instead of the
   parse-time-captured local, so the request carries the real 64-char token.
2. **Guard the response** — check `r.ok` before `r.json()`, so any non-OK
   response (e.g. a 403) shows a clear message instead of a JSON parse error.

## Files affected

- `templates/files/my_uploads.html` — `changed`

## Database changes

None.

## Effect on the current system

- Behavioral: bulk share (and per-row "Share") from My Files will succeed again.
  A failed request now surfaces a readable error instead of a JSON.parse crash.
- Backward-compatible: no API/contract change; only the client send + error path.
- Services to restart: none. Template change is picked up on next request
  (dev runserver autoreloads; on a server only `mmftp-web` serves templates, and
  template files are read per-render — a restart is optional, not required).
- Config/secrets: none.
- Rollback: revert the single template edit.

## Implementation detail (proposed edit)

In `templates/files/my_uploads.html`, in the bulk-share submit handler
(around line 285-287):

```js
var token = window.CSRF || csrf || '';
var r = await fetch("{% url 'files:bulk_share' %}", {
  method:'POST',
  headers:{'X-CSRFToken': token, 'Content-Type':'application/json'},
  body: JSON.stringify(payload)
});
if(!r.ok){
  btn.disabled=false; btn.textContent='Share';
  var e1=document.getElementById('bsErr');
  e1.textContent = (r.status===403)
    ? 'Your session expired or the page is stale. Please refresh and try again.'
    : 'Share failed (HTTP '+r.status+'). Please try again.';
  e1.hidden=false;
  return;
}
var d = await r.json();
```

(The parse-time `var csrf = window.CSRF || ''` at line 185 can stay as a harmless
fallback, or be removed — the fetch now prefers the live `window.CSRF`.)
