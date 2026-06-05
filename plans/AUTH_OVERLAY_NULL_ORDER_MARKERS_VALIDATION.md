# Auth Overlay NULL-Order Markers Validation

Plan reference: `plans/AUTH_OVERLAY_NULL_ORDER_MARKERS_PLAN.md`

## Requirement Status

- Preserve migration behavior that treats NULL-order markers as current-cycle
  markers when the release cycle floor is `-1`: Complete. The migration was not
  changed.
- Make worker candidate listing detect legacy NULL-order pending markers when
  the release cycle floor is `-1`: Complete. Added a regression test covering a
  NULL-order release and NULL-order pending marker.
- Make worker pending-attempt counts include legacy NULL-order pending markers
  when the release cycle floor is `-1`: Complete. The same regression asserts
  the pending count is `1`.
- Make worker terminal-marker checks include legacy NULL-order resolved or
  exhausted markers when the release cycle floor is `-1`: Complete. Added a
  regression test covering a NULL-order resolved marker.
- Preserve existing current-cycle scoping for modern non-NULL release floors:
  Complete. The existing focused auth-overlay retry test file still passes.
- Run focused tests only: Complete. Full AWF/GitHub validation remains managed
  by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/control/worker/cleanup_auth_overlay.py`
- `tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`
- `plans/AUTH_OVERLAY_NULL_ORDER_MARKERS_PLAN.md`
- `plans/AUTH_OVERLAY_NULL_ORDER_MARKERS_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q -k 'legacy_null_order'`
  - Before implementation: failed with candidate not listed and terminal marker
    not detected.
  - After implementation: `2 passed, 25 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py -q`
  - Result: `27 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup_auth_overlay.py tests/unit/control/test_cleanup_auth_overlay_retry_parts/test_cleanup_auth_overlay_retry_part_002.py`
  - Result: `All checks passed!`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker/cleanup_auth_overlay.py`
  - Result: `Success: no issues found in 1 source file`.

## Gaps

None for the planned scope. Broad validation, coverage gates, and CI-equivalent
checks were intentionally not run in this AWF agent phase.
