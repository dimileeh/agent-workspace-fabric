# PRRT_kwDOSJAM6s6HWUeJ Plan

## Problem Statement And Scope

The auth-overlay unmount backfill can seed a pending marker for a historical
effective `terminal_runtime_released` event whose `event_order` is `NULL`.
The deferred retry candidate query normalizes that release-cycle floor to `-1`,
but the marker-write cycle guard currently compares the listed `-1` floor to
the async latest-release reader's `None` value. That treats the same null-order
release cycle as revoked/replaced and skips the terminal marker after a
successful teardown.

Scope is limited to preserving the existing release-cycle guard while making
the null-order release case resolvable.

## Requirements Checklist

- Add a focused regression for a backfilled null-`event_order` release that is
  retried and resolved.
- Normalize null latest release orders consistently with the candidate query's
  `-1` floor when checking whether the release cycle is unchanged.
- Preserve stale-cycle protection for genuine revoke-plus-re-release cases.
- Run only targeted tests for the changed behavior; full AWF/GitHub validation
  remains managed after agent completion.

## Implementation Steps

1. Extend the existing Postgres auth-overlay retry migration test to cover a
   historical release event with `event_order = NULL`.
2. Confirm the new regression fails against the current guard.
3. Update the release-cycle comparison to treat `None` from the latest-release
   reader as the same `-1` floor used by the candidate query.
4. Re-run the focused regression and nearby cycle-guard tests.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6HWUeJ_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k "backfill_seeds_pre_upgrade_failed_release_and_retry_sweep_resolves or null_order_release_retry_sweep_resolves or record_resolved_skips_when_release_cycle_changed or record_exhausted_skips_when_release_cycle_changed or append_pending_skips_when_release_cycle_changed or record_resolved_writes_when_release_cycle_matches or pending_candidate_query_carries_release_cycle_floor"`
  - Passes after the fix; the added null-order regression fails before the fix
    by missing the resolved marker.
