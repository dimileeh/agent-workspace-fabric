# Review 4495131102 Deduplication Plan

## Problem Statement And Scope

PR review comment `issue:4495131102` identified two duplicated private helpers:

- `_default_dind_slots_from_profile` exists in both `src/awf/control/worker.py` and `src/awf/service/metrics.py`.
- `_empty_resource_reservation_totals` exists in both `src/awf/db/repositories.py` and `src/awf/service/metrics.py`.

The fix is limited to moving/reusing these helper behaviors from a single implementation point without changing capacity scheduling semantics or metrics output.

## Requirements Checklist

- [ ] Add regression coverage for the shared DinD default helper.
- [ ] Move the DinD default-profile heuristic to a shared capacity module and import it from scheduler and metrics code.
- [ ] Reuse the repository-level empty reservation totals helper from metrics instead of keeping a duplicate implementation.
- [ ] Preserve existing resource capacity and scheduler behavior.
- [ ] Run narrow verification that covers the changed helper paths.

## Implementation Steps

1. Add tests in the resource capacity unit suite for DinD default-slot detection.
2. Add `default_dind_slots_from_profile` to `src/awf/service/resource_capacity.py`.
3. Replace the local worker and metrics DinD helper definitions with imports from `resource_capacity`.
4. Rename the repository empty totals helper to a public module function and update repository and metrics callers to use it.
5. Run targeted tests and lint/type checks if the narrow tests pass.

## Assumptions/Changes

- `tests/unit/api/test_metrics_capacity.py` currently contains an unrelated failing scoping assertion for allocated resources. The unchanged `HEAD` query already includes reservation-node matches, so this cleanup will verify the touched helper paths with targeted tests instead of treating that broader behavioral failure as part of this review comment.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_resource_capacity.py tests/unit/db/test_resource_reservation_totals.py tests/unit/api/test_metrics_capacity.py::test_active_latest_totals_for_workspace_scope_delegates_to_repository -q`
  - Passes with all tests green.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/db/repositories.py src/awf/service/metrics.py src/awf/service/resource_capacity.py tests/unit/service/test_resource_capacity.py tests/unit/db/test_resource_reservation_totals.py`
  - Passes with no lint findings in touched surfaces.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes with no type errors.
