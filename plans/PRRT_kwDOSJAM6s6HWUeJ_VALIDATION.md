# PRRT_kwDOSJAM6s6HWUeJ Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6HWUeJ_PLAN.md`

## Requirement Status

- Complete: Added a focused regression for a backfilled null-`event_order`
  release that is retried and resolved.
- Complete: Normalized the async latest-release order to the same `-1` floor
  used by the candidate query when checking release-cycle sameness.
- Complete: Preserved stale-cycle protection for genuine
  revoke-plus-re-release cases by keeping the existing floor comparison for
  non-null latest release orders.
- Complete: Ran focused local checks only. Full AWF/GitHub validation is
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/control/worker/cleanup_auth_overlay.py`
- `tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`
- `plans/PRRT_kwDOSJAM6s6HWUeJ_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6HWUeJ_VALIDATION.md`

Commands run:

- Pre-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k "null_order_release_retry_sweep_resolves"`
  - Expected failure observed: resolved marker was absent and the release was
    logged as `TERMINAL_AUTH_OVERLAY_UNMOUNT_RELEASE_REVOKED`.
- Post-fix regression check:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k "null_order_release_retry_sweep_resolves"`
  - Passed: `1 passed, 24 deselected`.
- Focused behavior slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k "backfill_seeds_pre_upgrade_failed_release_and_retry_sweep_resolves or null_order_release_retry_sweep_resolves or record_resolved_skips_when_release_cycle_changed or record_exhausted_skips_when_release_cycle_changed or append_pending_skips_when_release_cycle_changed or record_resolved_writes_when_release_cycle_matches or pending_candidate_query_carries_release_cycle_floor"`
  - Passed: `7 passed, 18 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup_auth_overlay.py tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`
  - Passed: `All checks passed!`.

## Gaps

No planned gaps remain. Full repository validation, coverage, and CI-equivalent
checks are intentionally left to AWF/GitHub after agent completion.
