# Plan — Hide the Save button until there are unsaved changes (Figma-style)

**Status:** completed

## Summary
On the share-link settings panel the "Save link settings" button is always
visible. Replace it with a dirty-state bar that stays hidden until the user edits
something, then reveals **"Unsaved changes"** with **Discard** + **Save** actions
(like Figma). CSS + JS only; no backend changes.

## Behaviour
- On load, snapshot the settings form's current values.
- Any edit (link name, the five sliders, restrict checkbox, password, expiry date,
  download limit, or the email chips) recomputes a snapshot; if it differs from the
  initial one, the bar appears — if it matches again (user reverted), it hides.
- **Save** = the existing submit button (keeps the current submit handler that
  syncs `preview_only`, `clear_password`, chips, expiry/limit clearing).
- **Discard** = reload the page, restoring the server-rendered state (nothing was
  saved, so a reload is the clean, reliable revert).

## Files affected
- `templates/base.html` — **changed**. Add `.sld-savebar` CSS (hidden by default;
  message + button row; subtle reveal).
- `templates/files/share_link_detail.html` — **changed**.
  - Replace the standalone `<button>Save link settings</button>` with a
    `#sldSaveBar` block (`hidden` initially): "Unsaved changes" label, a Discard
    button (`type=button`), and the Save submit button.
  - Add dirty-tracking JS in the existing page `<script>`: build a value snapshot
    of the tracked controls, listen on the form's `input`/`change` events, expose a
    `window.sldRecheck()` the chips `render()` calls after it updates the hidden
    field, and show/hide the bar accordingly. Wire Discard → `location.reload()`.

## Database changes
None.

## Effect on the current system
- Behavioral: Save still posts the same data to the same endpoint; only its
  visibility changes (gated on the form being dirty). Discard performs a plain page
  reload (no server call). No new fields, no contract change.
- Backward-compatibility: full. If JS is disabled the bar would stay hidden — but
  the whole settings panel already relies on JS (chips, reveals, preview_only sync),
  so this matches existing assumptions; acceptable for this internal app.
- Scope: confined to this page + one shared CSS class. Other pages unaffected.
- Services: `mmftp-web` restart in prod / dev autoreload. No DB/jobs.
- Rollback: revert the two template files.

## Verification (manual)
- Load a link's settings → no Save button visible.
- Edit any control (toggle, name, add/remove a chip, set expiry…) → "Unsaved
  changes / Discard / Save" appears.
- Revert the edit manually → bar hides again.
- Save → settings persist (existing behaviour). Discard → page reloads to saved
  state and the bar is gone.
