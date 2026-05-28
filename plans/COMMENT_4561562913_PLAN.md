# Comment 4561562913 Compose Failure Resume Plan

## Problem Statement

Greptile's review-level comment on PR #292 flagged that
`resume_pr_monitor` returns after required companion-secret precheck failures
but continues after `ensure_project_up` raises `ComposeOperationError`.

## Scope

- Confirm whether continuing after compose restart failure is intentional.
- Preserve existing regression-tested monitor recovery behavior if intentional.
- Add a concise code comment explaining the intentional asymmetry.
- Avoid broad AWF/GitHub-owned validation; run only targeted local tests for the
  touched resume path.

## Requirements Checklist

- [x] Existing tests that encode compose-failure continuation remain intact.
- [x] `src/awf/control/executor/monitor_handoff.py` documents why the monitor
      may still run after `ComposeOperationError`.
- [x] No raw secrets, branch changes, pushes, or broad validation are introduced.
- [x] Verification evidence records targeted checks only; full AWF/GitHub
      validation remains post-agent owned.

## Implementation Steps

1. Inspect the existing resume monitor tests and implementation.
2. Add a short explanatory comment beside the `ComposeOperationError` handler.
3. Run the focused unit tests that assert compose failures are recorded while
   monitor recovery continues.
4. Save validation results in `plans/COMMENT_4561562913_VALIDATION.md`.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "resume_pr_monitor_compose_failure"`

Full AWF/GitHub validation remains owned by AWF after agent completion.
