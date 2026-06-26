# Plan — Internal scroll for the Recent activity list

**Status:** completed

## Summary
The "Recent activity" card grows unbounded as events accumulate. Wrap the activity
rows in a scroll container capped at ~4 entries tall (`max-height` + `overflow-y:auto`),
so the card stays a fixed size and scrolls internally. CSS + small markup wrap only.
User requested in-session → message is the approval.

## Files affected
- `templates/files/share_link_detail.html` — **changed**. Wrap the `{% for a in
  activity %}` rows in `<div class="sld-actwrap">…</div>` (the empty-state stays
  outside the scroll wrap). Add the `.sld-actwrap` rule in the page's `<style>`.

## Database changes
None.

## Effect on the current system
Visual only: the activity card is height-capped (~4 rows) and scrolls; no data,
behavior, or contract change. Dev autoreload / `mmftp-web` restart in prod.
Rollback = revert the file.
