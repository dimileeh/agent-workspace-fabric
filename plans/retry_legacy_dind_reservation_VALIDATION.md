# Retry Legacy DinD Reservation Validation

Plan reference: `plans/retry_legacy_dind_reservation_PLAN.md`

## Requirement Status

- Complete: Added a regression test for retrying a failed legacy source with no
  reservation and a stored DinD resolved profile.
- Complete: Preserved fallback reservation row creation for node assignment and
  host-port admission scoping.
- Complete: Fallback retry reservation now sets `dind_slots` and `dind_mode`
  from the stored resolved profile when no source reservation exists.
- Complete: Non-DinD fallback retries still derive zero DinD demand because any
  non-`dind` profile mode maps to `dind_slots=0`.
- Complete: Ran only focused local validation; broad AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/service/workspaces_retry.py`
- `tests/unit/service/test_workspace_retry.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `plans/retry_legacy_dind_reservation_PLAN.md`
- `plans/retry_legacy_dind_reservation_VALIDATION.md`

Focused checks:

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_legacy_dind_source_without_reservation_preserves_dind_demand -q`
  failed with `assert 0 == 1` for `retried_reservation.dind_slots`.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_legacy_dind_source_without_reservation_preserves_dind_demand -q`
- Passing nearby regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_recomputes_resource_reservation_from_current_defaults -q`
- Passing no-reservation host-port fallback coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_persist_reservation_when_source_has_none -q`
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry.py tests/unit/service/test_workspace_retry_port.py`

## Gaps

No planned gaps remain.
