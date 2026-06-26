# Change log — Dirty-state save bar on the share-link settings panel

**Plan:** `plans/20260626_101803_sharelink-dirty-save-bar.md`
**Date:** 2026-06-26

## What changed

UI-only. No view/model/migration changes.

### `templates/base.html`
- Added `.sld-savebar` CSS: hidden by default (`[hidden]`), amber-tinted bar with a
  flex "Unsaved changes" message + a right-aligned Discard/Save button row, and a
  small slide-in `@keyframes sldSaveIn` reveal. Placed just above the `.sld-zones`
  rules.

### `templates/files/share_link_detail.html`
- Replaced the always-visible `<button>Save link settings</button>` with a
  `#sldSaveBar` block (initially `hidden`): "Unsaved changes" label, a Discard
  button (`type=button`), and the Save submit button (still inside `#sldForm`, so
  the existing submit handler runs unchanged).
- Added a dirty-tracking IIFE in the page `<script>`: snapshots the tracked
  controls on load (link name, the five sliders by id, restrict checkbox, the
  hidden `allowed_emails`, password, expiry date, download limit), recomputes on
  the form's `input`/`change` events, and shows the bar only when the current
  snapshot differs from the initial one (reverting an edit re-hides it). Discard →
  `location.reload()` (clean revert to saved server state).
- The chips `render()` now calls `window.sldRecheck()` after updating the hidden
  field so chip add/remove participates in dirty detection. The chips seed runs
  before the dirty tracker initializes, so server-rendered emails are part of the
  baseline and don't trigger a false "unsaved" state (guarded by
  `if(window.sldRecheck)`).

## Deviations from plan
None. In-place placement (the approved default), not a sticky/floating bar.

## Verification
Pending manual check on a running stack (no automated suite). Suggested: load a
link → no Save button; edit any control or add/remove a chip → bar appears; revert
the edit → bar hides; Save persists; Discard reloads to saved state.
