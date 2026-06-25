# Require a recipient when "Specific users & groups" is chosen

**Status:** completed

## Summary

In the upload wizard's *Recipients* step, choosing **"Specific users & groups"**
but adding **no one** currently still lets the user click the action button and
"send" the file. The button silently relabels itself to *"Save to My Files"* and
the file is saved **Private** with no recipients — no validation fires, even
though the on-screen hint says *"Add at least one recipient or group, or switch
to Public link above."*

This is confusing: the user explicitly expressed intent to **share** with
specific people, but the file quietly becomes private. We want to **enforce**
that intent — if "Specific users & groups" is selected, at least one recipient
(user or group) must be added before the upload can be finalized. Saving with no
share target remains possible by explicitly choosing the **Private** option.

Root cause (current, intentional-but-confusing behavior):
- `templates/_upload_modal.html` — `updateSendLabel()` relabels the Send button
  to "Save to My Files" when `mode === 'users'` and `recipients.length === 0`,
  and the `uwSend` click handler treats recipients as optional
  (`recipients: mode === 'users' ? recipients.map(...) : []`).
- `apps/files/views.py::upload_finalize` — comment "Recipients are OPTIONAL"; it
  activates files regardless and cannot tell "Private" from
  "users-mode-but-empty" because the payload sends `recipients: []` for both.

## Fix

1. **Frontend (primary)** — in `templates/_upload_modal.html`:
   - When the access mode is `users` and `recipients.length === 0`, **disable**
     the Send button instead of relabeling it to "Save to My Files", and make the
     existing hint read as a required prompt (e.g. emphasize it). When at least
     one recipient is present, the button enables and reads "Send Files ✓".
   - Add the disable/enable check to `updateSendLabel()` / `syncAccessUI()` /
     `renderChips()` so toggling the radio or adding/removing chips updates the
     button live.
   - As a guard, the `uwSend` click handler returns early with an inline error if
     `mode === 'users' && recipients.length === 0`.
   - The **Private** radio remains the explicit "just save to My Files" path
     (unchanged), and **Public link** remains valid with zero named recipients
     (unchanged).

2. **Backend (defense in depth)** — in `apps/files/views.py::upload_finalize`:
   - Include the access mode in the JSON payload (`access_mode`: one of
     `private` / `users` / `public`) sent by the modal.
   - Reject with `400 {"error": "Add at least one recipient, or choose Private."}`
     when `access_mode === "users"`, no recipients resolved, and not public — so
     a direct API call can't reproduce the silent-private behavior either.

## Files affected

- `templates/_upload_modal.html` — changed (JS validation + button enable/disable;
  add `access_mode` to the finalize payload).
- `apps/files/views.py` — changed (server-side guard in `upload_finalize`).

## Database changes

None. No models, fields, migrations, or indexes are touched.

## Effect on the current system

- **Behavioral:** Users who pick "Specific users & groups" must add ≥1 recipient
  before finalizing; otherwise the Send button stays disabled with a clear prompt.
  No change to the Private or Public-link flows.
- **Backward-compatibility:** Files already saved are unaffected. The new
  `access_mode` payload field is additive; the backend treats a missing
  `access_mode` as "no extra guard" so older/cached clients are not broken
  (they already gate via the unchanged frontend on reload).
- **Services to restart:** `mmftp-web` (Gunicorn) after deploy, to pick up the
  `views.py` change. Template change is also served by `mmftp-web`. No timer/job,
  migration, config, or secret changes.
- **Rollback:** Revert the two edited files and restart `mmftp-web`. No data or
  schema to undo.
