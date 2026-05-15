# Request Admission Prune Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6CZJal` reports that `RequestAdmissionLimiter._prune`
scans all buckets on every `admit` call. The scope is limited to preserving the
fixed-window limiter behavior while avoiding repeated full-bucket scans inside
the same rate-limit window.

## Requirements Checklist

- Verify no existing class state already avoids repeated same-window pruning.
- Add a regression test proving repeated admits in the same window do not rescan
  existing buckets.
- Prune stale buckets when the relevant `window_seconds` advances.
- Preserve existing admission decisions, metadata, and validation errors.
- Keep changes scoped to request admission code and focused tests.

## Implementation Steps

1. Add a unit test around `RequestAdmissionLimiter` with many existing buckets
   and repeated admits in the same window.
2. Confirm the new test fails against the current implementation.
3. Add limiter state tracking the last pruned window per `window_seconds`.
4. Use the state to skip prune scans unless `current_window` advances.
5. Run targeted unit tests and static checks for the touched module.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py tests/unit/api/test_deps.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if practical for touched typed code.
