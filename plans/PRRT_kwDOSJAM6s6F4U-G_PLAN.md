# PRRT_kwDOSJAM6s6F4U-G Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6F4U-G` reports that custom profile planning artifact globs such as `docs/{workspace_id}.md` currently match shorthand names like `docs/ws_123.md`. Real generated workspace IDs are `ws_` plus 24 lowercase hex characters, so custom artifact matching should not downgrade real owned files with shorthand names to advisory plan-artifact overlap.

Scope is limited to owned-path plan artifact classification and focused regressions for custom profile matching.

## Requirements Checklist

- Custom `{workspace_id}` wildcard artifact paths match only real generated workspace IDs.
- Shorthand `ws_123` matching remains supported for the reserved default `docs/awf-plans` artifact classifier.
- Staleness treats a target-branch change to a custom-profile-owned real shorthand path as blocking `STALE_OVERLAP`, not advisory plan-artifact overlap.
- Existing concrete workspace-id matching and repeated-placeholder consistency remain intact.

## Implementation Steps

1. Update focused regression tests to encode strict custom-profile matching and the staleness behavior from the review thread.
2. Confirm the new/updated regression fails against the current implementation.
3. Remove shorthand suffix support from custom workspace-id glob regex construction.
4. Run focused tests for owned-path classification and the affected staleness scenario.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_staleness_parts/test_staleness_part_002.py -k "custom_sibling_plan_artifact_refresh_is_advisory_without_stale_candidate or custom_plan_path_shorthand_target_change_is_blocking_overlap" -q`

Both commands should pass. Full AWF/GitHub validation remains managed by AWF after agent completion.
