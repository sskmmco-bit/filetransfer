# Require a recipient when "Specific users & groups" is chosen

**Plan:** `plans/20260625_162906_require-recipient-for-users-access.md`
**Date:** 2026-06-25

## What changed

The upload wizard no longer silently saves a file as Private when the user picks
**"Specific users & groups"** but adds no recipients. That intent now requires at
least one recipient before the file can be finalized. The **Private** option
remains the explicit "save to My Files with no one" path, and **Public link**
still works with zero named recipients.

### `templates/_upload_modal.html` (changed)
- Added `let sending` flag to track an in-flight finalize request.
- Added `sendBlocked()` — true when access mode is `users` and `recipients`
  is empty.
- Reworked `updateSendLabel()`: label is "Save to My Files" only for the Private
  mode, "Send Files ✓" otherwise; the Send button is now **disabled** while
  `sendBlocked()` is true, and the recipient hint gets the `uw-req` class
  (red/bold) to read as a required prompt. Returns early while `sending` so the
  "Saving…" label/disabled state is preserved mid-request.
- Added a `.uw-req` CSS rule and an `id="uwUsersHint"` on the recipient hint.
- `uwSend` click handler: early-returns with an inline error if `sendBlocked()`;
  sets/clears the `sending` flag; error paths now reset via `sending=false;
  updateSendLabel()` instead of force-enabling the button.
- Added `access_mode: mode` to the finalize JSON payload.
- `reset()` clears `sending`.

### `apps/files/views.py` (changed)
- `upload_finalize`: rejects with `400 {"error": "Add at least one recipient,
  or choose Private."}` when the payload's `access_mode == "users"`, the upload
  is not public, and no recipients/groups resolved. Defense-in-depth mirroring
  the frontend guard. A missing `access_mode` (older clients) skips the guard,
  preserving backward compatibility.

## Database changes

None.

## Deviations from the plan

None. Implemented as planned.

## Follow-up / deploy notes

- Restart `mmftp-web` (Gunicorn) to pick up the `views.py` change.
- No migrations, timers, config, or secrets affected.
