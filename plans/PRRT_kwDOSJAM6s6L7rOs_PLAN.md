# PRRT_kwDOSJAM6s6L7rOs Plan

## Problem Statement

The upgrade guide says to reuse the `.env` created during first run and also
says `auto_cleanup_orphans` now defaults to enabled. Existing `.env` files
seeded from the old template can still contain
`AWF_AUTO_CLEANUP_ORPHANS=false`, which keeps upgraded services in report-only
orphan cleanup mode unless the operator changes that value.

## Scope

- Address only the inline review thread on `docs/UPGRADE.md`.
- Keep the fix documentation-only unless code inspection shows an existing
  migration already resolves the old seeded value.
- Do not run broad AWF/GitHub-owned validation.

## Requirements

- Verify whether existing code already migrates old seeded
  `AWF_AUTO_CLEANUP_ORPHANS=false` values.
- If not already fixed, update the upgrade guide to tell operators how to enable
  the new default when reusing an old `.env`.
- Preserve `AWF_AUTO_CLEANUP_ORPHANS=false` as the explicit report-only
  kill-switch.

## Implementation Steps

1. Inspect the upgrade guide and local service env handling.
2. Add a concise upgrade note near the orphan cleanup section.
3. Validate the diff with focused checks only.

## Verification

- `git diff --check`
- Manual inspection of the rendered Markdown diff.

Full AWF/GitHub validation is managed by AWF after agent completion.
