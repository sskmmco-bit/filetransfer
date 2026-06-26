# Plan — Standard toggles + email-chips for the share-link Access control

**Status:** completed

## Summary

Refine the share-link settings panel based on review feedback. Two themes:

### A. Revert the segmented OFF|ON pills to standard slider toggles
The OFF|ON text is redundant — the slider already communicates state. Switch all
five controls back to the existing reusable `.switch` slider (Email verification,
Allow downloads, Require password, Expiration date, Download limit), so the page
stays consistent. (Applying to **all five**, not just Email verification, so the
panel doesn't end up with mixed toggle styles — flag at approval if you only want
Email verification changed.)

### B. Modernise the "Allowed emails" input (Access control group)
Replace the textarea with an **email-chips** control like Gmail/GitHub recipients:
- Type an email + Enter / comma / blur → adds a chip with a ✕ remove button.
- **Per-email validation**: valid chips show a ✓ and neutral style; malformed
  entries show a ⚠ and a warning style.
- **Count badge** in the label: "Allowed emails (N)".
- Reworded checkbox: "Restrict to specific emails" → **"Allow only these email
  addresses"**.
- Shorter helper text: **"Only these email addresses can open this link."**
- More vertical spacing/dividers between the toggle row and the chips block so it
  feels less cramped.

## Backend contract preserved (no server changes)

The chips are front-end only. A **hidden `<input name="allowed_emails">`** is kept
in sync (comma-joined, valid chips only) on every add/remove and on submit. The
view (`apps/files/views.py::_parse_link_settings`) still reads `restrict_emails`
(checkbox) + `allowed_emails` (string) and runs it through
`sharelinks.normalize_emails` (already splits on comma/space/newline, lowercases,
dedupes, drops anything without "@"). So no view/model/migration changes.

- Initial chips render server-side from `link.allowed_emails` (a JSON list).
- `restrict_emails` checkbox still gates whether the whitelist is honoured and
  still drives the existing `data-toggle`/`data-reveal` show/hide.

## Files affected

- `templates/base.html` — **changed**.
  - Remove the now-unused `.lnk-pill` CSS (added earlier this session; only used on
    this page).
  - Add `.lnk-chips` component CSS (chip, chip.invalid, remove ✕, inline input,
    count badge) + a little extra spacing for the access-control reveal block.
- `templates/files/share_link_detail.html` — **changed**.
  - Swap the five `.lnk-pill` blocks back to `<label class="switch">…<span
    class="slider"></span></label>` (same `name`/`id`/`data-toggle` attrs, so the
    reveal wiring and form POST are unchanged).
  - Remove the hidden `preview_only` driver? **No** — keep it: the slider for
    "Allow downloads" is still the inverse of `preview_only`, so the hidden field +
    the one submit-handler line stay exactly as they are.
  - Replace the `allowed_emails` textarea with the chips markup: a hidden
    `allowed_emails` input, a chips container seeded from `link.allowed_emails`, an
    inline text input, the "Allowed emails (N)" label, and the shorter helper.
  - Add a `<script>` (in the existing page script block) implementing add/remove,
    validation, hidden-field sync, and count-badge update.

## Database changes
None.

## Effect on the current system
- **Behavioral impact:** none server-side; identical data posted to the same
  endpoint. Email-restriction behaviour is unchanged (still gated by
  `restrict_emails`, still normalized server-side which independently drops
  invalid entries — client validation is a UX aid, not the source of truth).
- **Backward-compatibility:** existing links with `allowed_emails` render their
  addresses as chips on load. Links without are unaffected.
- **Scope:** changes confined to this page + shared CSS additions/removal. The
  `.switch` slider is already used by other pages and is untouched. Removing
  `.lnk-pill` affects only this page (sole user).
- **Services:** `mmftp-web` restart in prod / dev autoreload. No jobs/DB.
- **Rollback:** revert the two template files.

## Verification (manual, post-implementation)
- Toggles render as sliders reflecting current state; Allow-downloads still maps to
  preview-only correctly on save.
- Enable "Allow only these email addresses", add several emails (Enter/comma),
  remove one, add a malformed one (shows ⚠), Save → reload → valid chips persist,
  malformed one dropped; count badge matches.
- Public page still enforces the whitelist (only listed emails can verify).
