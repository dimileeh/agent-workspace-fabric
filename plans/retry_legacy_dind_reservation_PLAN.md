# Retry Legacy DinD Reservation Plan

## Problem Statement And Scope

An inline review on PR #328 reports that `retry_workspace_row` creates a fallback
`ResourceReservation` with `dind_slots=0` when the source workspace has no prior
reservation, even if `source.resolved_profile` declares `docker.mode: dind`.
Because worker capacity admission treats an existing reservation as authoritative,
legacy retries can be claimed as non-DinD until provisioning later reconciles the
reservation.

Scope is limited to retry fallback reservation behavior and a focused regression
test. Broad AWF/GitHub validation remains owned by AWF after agent completion.

## Requirements Checklist

- Add a regression test for retrying a failed legacy source with no reservation
  and a stored DinD resolved profile.
- Preserve the fallback reservation row for node assignment and host-port
  admission scoping.
- Set fallback retry `dind_slots` and `dind_mode` from the stored resolved
  profile when no source reservation exists.
- Keep non-DinD legacy fallback retries at zero DinD demand.
- Run only focused local validation for the changed behavior.

## Implementation Steps

1. Add a test in `tests/unit/service/test_workspace_retry.py` that removes the
   source reservation, marks the source profile as DinD, retries it, and asserts
   the retry reservation records one DinD slot.
2. Run the new focused test and confirm it fails against current code.
3. Update `src/awf/service/workspaces_retry.py` fallback reservation construction
   to derive DinD mode from `source.resolved_profile`.
4. Run the focused retry test and a nearby retry reservation test.
5. Record validation evidence in
   `plans/retry_legacy_dind_reservation_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_legacy_dind_source_without_reservation_preserves_dind_demand -q`
  - Passes after the implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry.py::test_retry_recomputes_resource_reservation_from_current_defaults -q`
  - Existing nearby retry reservation behavior still passes.

## Assumptions/Changes

- During implementation, an existing no-reservation host-port fallback test was
  tightened to assert non-DinD fallback reservations remain at `dind_slots=0`.
  This stays within the planned retry fallback reservation scope.
