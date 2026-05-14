# Review Comment 4445667428 Failure Causality Plan

## Problem Statement And Scope

Greptile's review-level comment reports two failure-causality issues:

- `secondary_failures` grows without bound as repeated stale scan, stranding, or cleanup failures preserve the same primary failure.
- Primary-preserving failure transitions must keep `workspace.failure_reason` and `workspace.failure_message` populated from the primary snapshot, so later causality lookups do not lose the embedded primary.

The worker paths already restore primary row fields on this branch. This plan scopes the remaining implementation to:

- add a bounded secondary-failure history policy in `awf.service.failure_causality`;
- apply primary row-field restoration to the controls cleanup failure path;
- add focused regression coverage for both behaviors.

## Requirements Checklist

- [x] Bound `secondary_failures` to a small deterministic tail while preserving the latest `secondary_failure` field.
- [x] Preserve ordering of the retained secondary history.
- [x] Restore `workspace.failure_reason` and `workspace.failure_message` from primary evidence in the controls cleanup failure path when cleanup failure is secondary.
- [x] Keep the existing epoch-reset behavior that ignores stale embedded primary failures after a resume.
- [x] Add regression tests that fail without the implementation.
- [x] Run the narrowest relevant test commands and record results in validation.

## Implementation Steps

1. Add tests in `tests/unit/service/test_failure_causality.py` for capped secondary history.
2. Add or update controls test coverage to simulate an embedded primary with stale row-level failure fields, then verify cleanup failure restores primary row fields.
3. Implement a shared row-field restore helper for controls or use the same local pattern without changing worker behavior.
4. Implement a bounded secondary history tail in `build_preserved_failure_payload`.
5. Run focused unit tests for failure causality and controls.
6. Run relevant lint/type checks if the focused tests pass.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py -q`
  - Passes with new regression coverage.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py src/awf/service/controls.py tests/unit/service/test_failure_causality.py tests/unit/service/test_controls.py`
  - Passes without lint errors.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes or any pre-existing unrelated failure is documented.
