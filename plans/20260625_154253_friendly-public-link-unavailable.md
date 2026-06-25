# Friendly "link unavailable / expired" page for public share links

**Status:** completed

## Summary

When a public share link is disabled, expired, or has no active files, the public
views raise a bare `Http404("This link is invalid or has expired.")`. With
`DEBUG=True` this shows Django's raw yellow debug 404 page ("Raised by:
apps.public.views.landing", URLconf dump, etc.); with `DEBUG=False` it shows the
generic site 404. Neither is a good experience for an external recipient.

Replace this with a proper, branded page that clearly says the link is no longer
available — rendered with HTTP 404 so it displays correctly **in both DEBUG and
production** (a rendered response bypasses Django's debug 404 page).

Approach:
- Add a small `LinkUnavailable` exception in `apps/public/views.py`.
- Change the `_link()` and `_link_files()` helpers to raise `LinkUnavailable`
  instead of `Http404`.
- Add a `@public_link_view` decorator that wraps each public view and, on
  `LinkUnavailable`, renders the new template `public/unavailable.html` with
  `status=404`.
- Create `templates/public/unavailable.html` (extends `base.html`, styled like
  `public/denied.html`) with a friendly headline + explanation.

Out of scope (kept as-is): `_link_file()` / `get_object_or_404` for a missing
*individual* file uuid under an otherwise-valid link still raises `Http404`. That
is a rarer edge case; can be folded in later if desired.

## Files affected

- `templates/public/unavailable.html` — `created`
- `apps/public/views.py` — `changed` (add exception + decorator; update `_link`,
  `_link_files`; decorate the public views)

## Database changes

None.

## Effect on the current system

- Behavioral: disabled/expired/empty public links now show a clean "This link is
  no longer available" page (HTTP 404) instead of the debug/raw 404. Valid links
  are unaffected.
- Backward-compatible: still returns 404 status; only the rendered body changes.
- Services to restart: none (template + view code read per request; dev runserver
  autoreloads). On a server, `mmftp-web` picks up the Python change on its next
  restart, but no DB/migration work is required.
- Config/secrets: none.
- Rollback: revert the two files (delete the new template, revert views.py).

## Implementation detail (proposed)

`apps/public/views.py`:

```python
from functools import wraps

class LinkUnavailable(Exception):
    """A share link that is missing, disabled, expired, or has no active files."""

def public_link_view(view):
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        try:
            return view(request, *args, **kwargs)
        except LinkUnavailable:
            return render(request, "public/unavailable.html", status=404)
    return wrapped
```

- `_link()` / `_link_files()`: `raise LinkUnavailable(...)` instead of `Http404(...)`.
- Decorate each public view (`landing`, `submit_password`, `request_code`,
  `verify_code`, `preview`, `download`, `download_all`) with `@public_link_view`
  (placed under the existing `@require_http_methods`).

`templates/public/unavailable.html` — extends `base.html`, mirrors
`public/denied.html` styling, with a clear message such as:
"This link is no longer available — it may have expired, been disabled by the
sender, or the files were removed. Please ask the sender for a new link."
