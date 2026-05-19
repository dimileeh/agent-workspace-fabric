# Protected Quality Gate Full Diff Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6DSIY4` reports that `WorkspaceExecutor`
checks protected quality-gate files only against the staged delta in the
post-agent and validation-fix commit paths. If an agent self-commits an
out-of-scope protected file before AWF stages remaining work, that protected
edit can reach the normal push path without being reclassified against
`base_commit..HEAD`.

Scope is limited to executor-side protected quality-gate enforcement before
normal PR push. PR monitor push protection already has committed-diff coverage
and is not part of this change.

## Requirements Checklist

- Add a regression test for an agent self-committing a protected quality-gate
  edit before AWF stages and commits remaining allowed work.
- Preserve declared `owned_paths` behavior when classifying cumulative committed
  protected-file changes.
- Re-check committed output from `base_commit..HEAD` before the normal executor
  push path, after validation/fix-pass commits have settled.
- Fail with the existing `QUALITY_GATE_POLICY_CHANGED` policy failure rather
  than pushing or opening a PR.
- Keep changes minimal and avoid weakening existing quality-gate tests.

## Implementation Steps

1. Add a failing executor regression test in
   `tests/unit/control/test_executor_validation_fix_cycle.py`.
2. Add a small executor helper that classifies committed paths since
   `base_commit` using `_protected_file_diffs_for_committed_paths`.
3. Invoke that helper before transitioning from `validating` to `pushing` on
   the normal push path.
4. Update only affected test queues/helpers if the new pre-push git diff call
   changes command ordering.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestProtectedQualityGateChanges::test_initial_agent_self_committed_protected_change_before_staged_work_is_blocked -q`
  must fail before implementation and pass after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_validation_fix_cycle.py::TestQualityGatePolicy -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor.py tests/unit/control/test_executor_validation_fix_cycle.py`
  must pass.
