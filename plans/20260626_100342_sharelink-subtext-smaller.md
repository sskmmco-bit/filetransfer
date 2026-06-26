# Plan — Shrink the description sub-text on share-link settings rows

**Status:** completed

## Summary

Follow-up to `plans/20260626_095732_…`. The `.lnk-s` description text under each
setting title reads too large/cramped in the narrow settings column. Reduce its
font-size and tighten line-height so it sits as quieter helper text. CSS-only.

User explicitly requested this exact tweak in-session, so their message is the
approval.

## Files affected
- `templates/base.html` — **changed**. `.lnk-row .lnk-s` font-size 12px → 11px,
  line-height 1.4 → 1.45 (slightly looser to stay readable at the smaller size).

## Database changes
None.

## Effect on the current system
Purely visual; affects every `.lnk-row` description (share-link settings panel and
any share-create form using the same rows). No behavior, data, or contract change.
`mmftp-web` restart in prod / dev autoreload to see it. Rollback = revert the line.
