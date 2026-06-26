# Plan — Widen the right-hand Link settings column

**Status:** completed

## Summary
Follow-up to the share-link settings redesign. The right settings column feels a
bit narrow (text wraps tightly). Widen it from 340px to 380px. CSS-only, scoped to
this page. User requested in-session → message is the approval.

## Files affected
- `templates/files/share_link_detail.html` — **changed**. `.sld-grid`
  `grid-template-columns:1fr 340px` → `1fr 380px`.

## Database changes
None.

## Effect on the current system
Visual only: the left content column gives up 40px to the settings column on
viewports ≥900px (below that the layout already collapses to one column, unchanged).
No behavior/data/contract change. Dev autoreload / `mmftp-web` restart in prod.
Rollback = revert the value.
