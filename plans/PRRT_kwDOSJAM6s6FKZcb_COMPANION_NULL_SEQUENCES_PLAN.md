# PRRT_kwDOSJAM6s6FKZcb Companion Null Sequences Plan

## Problem Statement and Scope

The review thread reports that `src/awf/node/companion_services.py` iterates
optional companion sequence fields with `item.get("field", [])`, which still
returns `None` when persisted task policy data contains explicit nulls. This
can crash companion spec normalization for `depends_on`, `ports`, or `volumes`.

Scope is limited to companion task-policy normalization and focused unit
coverage for this review thread.

## Requirements Checklist

- Add a regression test showing explicit null `depends_on`, `ports`, and
  `volumes` normalize to empty tuples.
- Preserve existing validation for malformed non-null sequence entries.
- Change only companion normalization code needed for this thread.
- Run focused unit tests for the changed behavior.
- Do not run broad AWF/GitHub-owned validation; AWF owns that after agent exit.

## Implementation Steps

1. Add a failing unit test in `tests/unit/node/test_companion_services.py`.
2. Run the focused new test and confirm the current failure.
3. Update `src/awf/node/companion_services.py` so explicit `None` is treated as
   an empty sequence for `depends_on`, `ports`, and `volumes`.
4. Re-run focused companion service unit tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_treats_null_optional_sequences_as_empty -q`
  - First run should fail before implementation.
  - Final run should pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q`
  - Should pass after implementation.
