# Plan — Validate password when "Require password" is ON

**Status:** completed

## Summary
Bug: with the **Require password** toggle ON but the password field empty, Save
appears to succeed yet sets no password (the view parses `password=KEEP`, so the
hash is left untouched — and on reload the toggle flips back OFF). A whitespace-only
value (`" "`) would actually be stored as the password. Add validation so you can't
save a "required password" with no real value.

## Behaviour after fix
- **Require password ON + empty field + no existing password** → block Save, show
  an inline error under the field, focus it. (If a password is already set, leaving
  the field empty stays valid — it means "keep the current password".)
- Whitespace-only input counts as empty.
- Error clears as soon as the user types in the field or toggles Require password
  off.

## Files affected
- `templates/files/share_link_detail.html` — **changed**.
  - Give the password input an `id` and add an inline error element + `.lnk-err`
    style; pass `link.has_password` into the script.
  - In the existing form `submit` handler, take the event, and `preventDefault()`
    + show error + focus when ON/empty/no-existing. Clear the error on input/toggle.
- `apps/files/views.py` — **changed** (defense-in-depth). In
  `_parse_link_settings`, treat a blank/whitespace-only `password` as "not provided"
  (`(src.get("password") or "").strip()`), so a whitespace-only value can never be
  stored as the password.

## Database changes
None.

## Effect on the current system
- Behavioral: prevents the misleading empty-password save. Normal flows
  (set/change/keep/remove password) are unchanged. The server guard only affects
  the previously-degenerate whitespace-only case.
- Backward-compatibility: full; no field/contract changes. The view still accepts
  the same POST keys.
- Scope: this page's template + one parsing helper. No other callers of
  `_parse_link_settings` rely on whitespace passwords (create flow benefits too).
- Services: `mmftp-web` restart in prod (the view change) / dev autoreload.
- Rollback: revert the two files.

## Verification (manual)
- Toggle Require password ON, leave empty, Save → blocked with inline error; field
  focused. Enter a value → saves; public page prompts for it.
- Link with an existing password: open settings (toggle ON), leave field empty,
  Save → keeps the existing password (no error). Toggle OFF + Save → password removed.
- Whitespace-only input is rejected client-side; even if bypassed, the server no
  longer stores it.
