# Change log — Natural-language toggles + CTA hierarchy on the share-link settings page

**Plan:** `plans/20260626_095732_sharelink-settings-natural-language-toggles-cta.md`
**Date:** 2026-06-26

## What changed

UI-only redesign of the right-hand "Link settings" panel on the public share-link
detail page. No model/view/service/migration changes — the form posts the same
field names the view already parses.

### `templates/base.html`
- **Added** reusable `.lnk-pill` CSS: a segmented `OFF | ON` toggle (hidden
  checkbox + two `.seg` spans). Unchecked → OFF segment shaded; checked → ON
  segment lights up green (`var(--accent)`). Focus ring via `:focus-visible`.
  Placed right after the existing `.switch` rules, which are left intact (still
  used by `_upload_modal.html`, `my_uploads.html`, `detail.html`, `_edit_form.html`,
  `_manage_form_user.html`).
- **Strengthened the `.sld-zone` CTA tiers:** increased inter-zone spacing
  (`gap` 14px→26px, top padding 14px→20px), and gave each tier a distinct
  default tinted background so they no longer look alike:
  - Rotate (neutral) — unchanged outline.
  - Disable (`.sld-zone.warn`) — amber text/border + amber-bg fill; amber heading.
  - Delete (`.sld-zone.danger`) — red text/border + red-bg fill.
  Buttons also got slightly taller padding.

### `templates/files/share_link_detail.html`
- Replaced all five slider switches (`<label class="switch">`) with the new
  `.lnk-pill` markup. Each keeps its original `name`/`id`/`data-toggle`
  attributes, so the global toggle→reveal wiring and the form POST are unchanged.
- Reworded two titles for plain language:
  - "Preview only" → **"Allow downloads"** (pill checked = `link.allow_download`).
  - "Password protection" → **"Require password"**.
- Added a hidden `<input name="preview_only" id="sldPreviewOnly">` and one line in
  the existing form `submit` handler that sets it to the **inverse** of the
  Allow-downloads pill — preserving the `preview_only` contract the view expects
  (`apps/files/views.py::_parse_link_settings`, `allow_download = not preview_only`).
  Mirrors the existing `clear_password` pattern.
- Retitled the zone buttons to title case: **Rotate Token**, **Disable/Enable
  Link**, **Delete Link**. The `.sld-zone` / `.warn` / `.danger` wrapper classes
  were already present; they now pick up the strengthened tier styling.

## Deviations from plan

None. Implemented as planned. (Minor extra: button labels retitled to title case
for the CTA hierarchy, consistent with the plan's intent.)

## Verification

Pending manual check on a running stack (no automated test suite). Suggested:
toggle Allow-downloads / Require password / Expiration / Download-limit, save,
reload, and confirm persisted state + public-page behavior; confirm the three
zone buttons render neutral/amber/red with the new spacing.
