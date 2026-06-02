# PRRT_kwDOSJAM6s6GT82g Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6GT82g_PLAN.md`

## Requirement Status

- Reproduce the legacy retry shape with no source reservation, no
  `resolved_profile`, and inline `requested_profile` requesting DinD:
  Complete. Added
  `test_retry_legacy_inline_dind_source_without_resolved_profile_preserves_dind_demand`.
- Preserve DinD demand when creating the retry reservation for that legacy
  shape: Complete. The retry no-reservation fallback now falls back from
  missing `resolved_profile` to stored `requested_profile`.
- Keep existing behavior for retry sources that already have a reservation or a
  resolved profile: Complete for the touched surface. Adjacent DinD and
  non-DinD no-reservation retry tests pass.
- Run focused validation only: Complete. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

- Changed `src/awf/service/workspaces_retry.py`.
- Changed `tests/unit/service/test_workspace_retry.py`.
- Confirmed the new regression failed before the fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_legacy_inline_dind_source_without_resolved_profile_preserves_dind_demand -q`
- Focused tests after the fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_legacy_dind_source_without_reservation_preserves_dind_demand tests/unit/service/test_workspace_retry.py::test_retry_legacy_inline_dind_source_without_resolved_profile_preserves_dind_demand -q`
  passed with `2 passed`.
- Focused non-DinD regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_persist_reservation_when_source_has_none -q`
  passed with `1 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry.py`
  passed.
