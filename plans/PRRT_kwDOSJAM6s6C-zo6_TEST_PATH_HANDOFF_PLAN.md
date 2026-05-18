# PRRT_kwDOSJAM6s6C-zo6 Test Path Handoff Plan

## Problem Statement and Scope

The conformance validation handoff classifier allows `tests/` path mentions in
named validation command handoff gaps so commands such as `pytest tests/unit -q`
remain AWF-owned validation evidence. The review thread reports that mixed gaps
like "run pytest during validation and add tests/unit/..." can therefore be
misclassified as AWF-owned validation handoff even though creating or updating a
test file is deterministic agent work.

Scope is limited to `src/awf/runtime/planning.py`, its focused unit tests, and
this plan/validation record.

## Requirements Checklist

- Add a regression test showing named validation command handoff mixed with
  add/create/update test-path work remains agent-owned.
- Preserve existing acceptance for pure validation command handoff gaps that
  include test paths as command arguments.
- Update the classifier with the smallest change needed to distinguish
  test-path command arguments from deterministic test file work.
- Run focused validation for the changed behavior and linter coverage for the
  touched files.

## Implementation Steps

1. Add a failing unit test in `tests/unit/runtime/test_planning.py` for mixed
   named validation handoff plus test-path file work.
2. Confirm the new regression fails before implementation.
3. Update `_is_awf_validation_evidence_gap` so path-shaped `test/` or `tests/`
   mentions are only skipped when they are not tied to nearby deterministic
   file-work verbs.
4. Re-run the focused unit tests and ruff for the touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py::<new regression> -q`
  initially fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/planning.py tests/unit/runtime/test_planning.py`
  passes.
