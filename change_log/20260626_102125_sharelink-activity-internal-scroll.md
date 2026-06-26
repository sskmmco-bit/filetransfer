# Change log — Internal scroll for the Recent activity list

**Plan:** `plans/20260626_102125_sharelink-activity-internal-scroll.md`
**Date:** 2026-06-26

## What changed
- `templates/files/share_link_detail.html`:
  - Wrapped the Recent-activity `{% for %}` rows in `<div class="sld-actwrap">`,
    converting the old `{% empty %}` to an `{% if activity %} … {% else %} … {% endif %}`
    so the empty-state stays outside the scroll area.
  - Added `.sld-actwrap { max-height:370px; overflow-y:auto; … }` (≈4 rows) to the
    page `<style>`, so the card no longer grows unbounded — extra events scroll
    inside it.

## Deviations from plan
None (max-height tuned to 370px to fit ~4 full rows including IP + verified-email lines).
