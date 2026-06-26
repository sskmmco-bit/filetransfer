# Change log — Validate password when "Require password" is ON

**Plan:** `plans/20260626_102346_sharelink-require-password-validation.md`
**Date:** 2026-06-26

## What changed

### `apps/files/views.py` (defense-in-depth)
- `_parse_link_settings`: the "value sets the password" branch now requires a
  **non-blank** value — `elif (src.get("password") or "").strip():`. A
  whitespace-only password is treated as not provided (falls through to KEEP), so
  it can never be stored. Benefits both the update and create flows.

### `templates/files/share_link_detail.html`
- Password input given `id="sldPwd"`; added an inline `#sldPwdErr` error element
  and `.lnk-err` / `input.lnk-invalid` styles in the page `<style>`.
- Form `submit` handler now takes the event and **blocks submission** when Require
  password is ON, no password is already set, and the field is empty/whitespace —
  showing the error, marking the field invalid, and focusing it. An already-set
  password may still be left blank to keep it (no error).
- Added listeners that clear the error as soon as the user types in the field or
  toggles Require password.

## Deviations from plan
None.

## Notes
- The view change means **`mmftp-web` needs a restart in prod** to take effect;
  dev `runserver` autoreloads. Template/JS changes need only a reload.

## Verification
Pending manual check on a running stack (no automated suite): toggle Require
password ON + empty → Save blocked with inline error + focus; enter a value →
saves and the public page prompts; existing-password link with empty field → keeps
it (no error); toggle OFF + Save → removes it.
