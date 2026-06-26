# Plan — Natural-language toggles + CTA hierarchy on the share-link settings page

**Status:** completed

## Summary

Redesign the right-hand "Link settings" panel and the zone buttons on the
public share-link detail page so the controls read in plain language and the
maintenance/availability/danger actions have a clear visual hierarchy.

Two changes, both UI-only (no model, no view, no backend contract changes):

1. **Replace the slider switches with segmented `OFF | ON` pills.** Each control
   keeps its title + description, but the slider knob is replaced by a two-segment
   pill whose active side is highlighted (green for ON). State is unambiguous at a
   glance. Where it improves clarity, titles are reworded to positive phrasing:
   - "Preview only" → **"Allow downloads"** (ON = downloads allowed; default ON).
   - "Password protection" → **"Require password"**.
   - "Email verification", "Expiration date", "Download limit" keep their titles,
     just get the new pill control.

2. **Better CTA hierarchy for the zone actions.** Today *Rotate token*,
   *Disable link*, and *Delete link* look near-identical. Restyle them as a clear
   three-tier ladder with more spacing between zones:
   - **Rotate Token** — neutral (default outline).
   - **Disable Link** — amber/orange.
   - **Delete Link** — red.

### Backend contract is preserved (important)

The form still POSTs the exact same field names the view expects
(`apps/files/views.py::_parse_link_settings`): `require_verify`, `restrict_emails`,
`allowed_emails`, `preview_only`, `clear_password`, `password`, `expiry_date`,
`download_limit`. No view/service/model edits.

- The visible "Allow downloads" pill is the **inverse** of the posted
  `preview_only`. A hidden `preview_only` input is set in the existing form
  `submit` handler (same pattern already used for `clear_password`): when the
  Allow-downloads pill is OFF, post `preview_only=on`; otherwise empty.
- The reveal-on-toggle blocks (restrict-emails textarea, password field, expiry
  date, download-limit number) continue to show/hide via the existing
  `data-toggle`/`data-reveal` mechanism — the pill's hidden checkbox keeps the
  same `id`/`data-toggle` attributes, so the existing reveal JS keeps working
  unchanged.

## Files affected

- `templates/base.html` — **changed**. Add `.lnk-pill` (segmented OFF|ON toggle)
  CSS near the existing `.switch` rules; add the amber/red CTA tiers and extra
  spacing to the `.sld-zone` rules. (The old `.switch`/`.slider` rules stay —
  they're still used on other pages: `_upload_modal.html`, `my_uploads.html`,
  `detail.html`, `_edit_form.html`, `_manage_form_user.html`.)
- `templates/files/share_link_detail.html` — **changed**. Swap the five
  `<label class="switch">…</label>` controls for the new pill markup; reword the
  two titles; add the hidden `preview_only` input + one line in the existing
  submit handler to drive it from the Allow-downloads pill; regroup the three
  zone buttons with the new tier classes.

## Database changes

None. No models, fields, migrations, or indexes touched.

## Effect on the current system

- **Runtime/behavioral impact:** none functional. Same data is posted to the same
  endpoint; the link behaves identically. Purely presentational + clearer labels.
- **Backward-compatibility:** fully compatible. The "Allow downloads" relabel is a
  display inversion only; stored `allow_download` semantics and the `preview_only`
  POST field are unchanged. Existing links render correctly (a link with
  `allow_download=False` shows the Allow-downloads pill OFF / preview-only).
- **Scope containment:** the segmented-pill CSS is a new class (`.lnk-pill`), so no
  other page that uses `.switch` is affected. Only the share-link settings panel
  changes.
- **Services to restart:** only `mmftp-web` (Gunicorn) picks up template changes;
  in dev `runserver` autoreloads. No timers/jobs involved.
- **Config/secrets needed:** none.
- **Rollback:** revert the two template files (git). No data or schema to undo.

## Verification (manual, after approval + implementation)

- Open an existing active link's settings; confirm each pill reflects current state
  (Allow downloads ON for a normal link; Email verification matches
  `require_email_verify`; etc.).
- Toggle Allow-downloads OFF, Save → reload → link is preview-only (download
  blocked on the public page); toggle back ON → downloads work.
- Toggle Require password ON, set a password, Save → public page prompts; toggle
  OFF, Save → password removed.
- Expiration date / Download limit reveal + persist correctly.
- Confirm Rotate (neutral) / Disable (amber) / Delete (red) render with the
  intended colors and spacing; Delete still shows the confirm dialog.
