# PRRT_kwDOSJAM6s6B9sW9 Plan

## Problem Statement and Scope

The PR review reports that `restore_primary_failure_row_fields()` skips
assignment when preserved primary failure evidence has no `failure_reason`.
That can leave a stale row-level failure reason in place after a secondary
failure transition preserves a primary failure payload.

Scope is limited to the failure-causality helper and focused regression
coverage.

## Requirements Checklist

- [x] Add regression coverage for a preserved primary failure with no
  `failure_reason`.
- [x] Update `restore_primary_failure_row_fields()` so the row
  `failure_reason` exactly reflects the preserved primary failure value,
  including `None`.
- [x] Preserve existing bounded primary message restoration behavior.
- [x] Run focused tests and lint for the touched files.
- [x] Commit the local fix with a conventional commit referencing the thread.

## Implementation Steps

1. Extend `tests/unit/service/test_failure_causality.py` with a focused helper
   regression that starts with a stale row failure reason and restores primary
   evidence lacking `failure_reason`.
2. Run the new test before implementation to confirm the current failure.
3. Update `src/awf/service/failure_causality.py` to assign the normalized
   `failure_reason` unconditionally.
4. Run the focused test module and lint.
5. Write `plans/PRRT_kwDOSJAM6s6B9sW9_VALIDATION.md`.
6. Stage only changed files and commit locally.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py::test_restore_primary_failure_row_fields_clears_missing_failure_reason -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_failure_causality.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/failure_causality.py tests/unit/service/test_failure_causality.py
```

Pass criteria: the focused regression fails before the implementation change,
then all listed commands pass after the implementation change.
