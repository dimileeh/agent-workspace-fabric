# Remote Branch Fallback Review Plan

## Problem Statement And Scope

Address PR review thread `PRRT_kwDOSJAM6s6Du-Uh` for PR `#272`.

Preserved-active branch PR recovery chooses `remote_push_branch or branch_name`
before calling `_resolve_preserved_active_branch_open_pr`. A whitespace-only
`remote_push_branch` is truthy, so the lookup receives a branch that strips to
empty and returns `None` instead of falling back to `branch_name`.

Scope is limited to preserved-active branch PR lookup branch selection in
`src/awf/control/worker.py` and focused regression coverage in
`tests/unit/control/test_worker.py`.

## Requirements Checklist

- [ ] Treat whitespace-only `remote_push_branch` as blank when selecting the
  preserved-active open-PR lookup branch.
- [ ] Preserve existing behavior where a nonblank `remote_push_branch` takes
  precedence over `branch_name`.
- [ ] Preserve existing fallback behavior for `remote_push_branch=None`.
- [ ] Add or update regression tests before production code and confirm the
  focused test fails before implementation when practical.
- [ ] Run the narrow affected test and lint/type checks appropriate for the
  touched files.

## Implementation Steps

1. Update the existing branch-name fallback test so it parametrizes both
   `None` and whitespace-only `remote_push_branch`.
2. Run the whitespace-only case to confirm it fails against the current
   implementation.
3. Normalize `remote_push_branch` and `branch_name` before selecting the lookup
   branch for `_resolve_preserved_active_branch_open_pr`.
4. Run the focused regression test, then ruff on touched Python files.
5. Record validation evidence in the matching validation file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k 'preserved_active_pushed_branch_lookup_falls_back_to_branch_name'`
  - Fails before implementation for the whitespace-only case and passes after
    implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - No lint errors.
