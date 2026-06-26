# Change log — Standard toggles + email-chips for share-link Access control

**Plan:** `plans/20260626_101034_sharelink-standard-toggles-email-chips.md`
**Date:** 2026-06-26

## What changed

UI-only. No view/model/migration changes — same fields posted to the same endpoint.

### `templates/base.html`
- **Removed** the `.lnk-pill` segmented OFF|ON CSS (added earlier this session;
  this page was its only user). The `.switch` slider CSS is unchanged.
- **Added** `.lnk-chips` component CSS: the chips container (with `.focus` ring),
  `.lnk-chip` (valid = green/accent, `.invalid` = amber with ⚠), the ✕ remove
  button, the inline `.lnk-chip-input`, and the `.lnk-chips-label` + count badge
  (`.lnk-chips-count`).

### `templates/files/share_link_detail.html`
- Reverted all five controls (Email verification, Allow downloads, Require
  password, Expiration date, Download limit) from `.lnk-pill` back to the standard
  `<label class="switch"><span class="slider"></span></label>` slider. Same
  `name`/`id`/`data-toggle` attributes — reveal wiring and POST contract unchanged.
  The hidden `preview_only` driver + its submit-handler line are kept (Allow-
  downloads slider is still the inverse of `preview_only`).
- Access control group reworked:
  - Checkbox relabelled "Restrict to specific emails" → **"Allow only these email
    addresses"**.
  - Textarea replaced by an **email-chips** control: a hidden
    `<input name="allowed_emails">` (source of truth, comma-joined valid chips), a
    chips container seeded server-side from `link.allowed_emails`
    (`data-emails`), and an inline text input.
  - **"Allowed emails (N)"** count badge that updates live (hidden at 0).
  - Helper text shortened to **"Only these email addresses can open this link."**
  - Added a divider + extra spacing between the Email-verification toggle row and
    the allowed-emails block.
- Added chips JS in the existing page `<script>`: add on Enter/comma/blur, remove
  via ✕ (and Backspace on empty input), dedupe, per-email regex validation
  (valid → ✓, malformed → ⚠), live hidden-field sync + badge update, and a
  `window.sldSyncChips()` flush called on form submit so unsent typed text is
  committed. Only valid chips are written to the hidden field; the server's
  `normalize_emails` remains the authoritative filter.

## Deviations from plan
None. Implemented with the two approved defaults: all five toggles reverted to
sliders; invalid chips excluded from the submitted value.

## Verification
Pending manual check on a running stack (no automated suite). Suggested: enable
"Allow only these email addresses", add several emails (Enter/comma), remove one,
add a malformed one (shows ⚠), Save → reload → valid chips persist & badge matches,
malformed dropped; confirm sliders reflect state and Allow-downloads still maps to
preview-only on save; confirm the public page still enforces the whitelist.
