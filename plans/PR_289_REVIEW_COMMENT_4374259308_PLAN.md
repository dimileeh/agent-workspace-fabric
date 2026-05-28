# PR 289 Review Comment 4374259308 Plan

## Problem Statement and Scope

Address review-level comment `4374259308` for PR #289 by verifying the bundled
actionable findings against the current branch, fixing only still-valid issues,
and preserving already-covered regression behavior.

## Requirements Checklist

- Verify scalar `depends_on` handling in `src/awf/node/companion_services.py`.
- Verify scalar `depends_on` regression coverage in
  `tests/unit/node/test_companion_services.py`.
- Verify companion worktrees are marked skipped when primary worktree removal
  fails in `src/awf/service/gc.py`.
- Fix the remaining valid inline import nit in
  `tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py`.
- Run focused validation only for touched or directly relevant behavior.
- Commit the local fix without pushing or switching branches.

## Implementation Steps

1. Inspect current implementation and tests for the reported findings.
2. Leave stale findings unchanged when current code already satisfies them.
3. Move retry exception imports to the module-level import list and update the
   affected `pytest.raises` assertions.
4. Create a validation document recording requirement status and focused test
   evidence.
5. Stage only changed files and commit with a conventional message referencing
   comment `4374259308`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_specs_from_task_policy_treats_scalar_dependency_as_single_service -q`
  passes, confirming scalar dependency regression coverage.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspaces_observability_parts/test_workspaces_observability_part_001.py::test_retry_workspace_errors_and_missing_source_attempt_fallback -q`
  passes, confirming the edited retry assertions still work.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
