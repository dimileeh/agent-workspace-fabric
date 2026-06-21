# PRRT_kwDOSJAM6s6KxQa0 Plan

## Problem

Review thread `PRRT_kwDOSJAM6s6KxQa0` reports that validation fix-pass
worktree-availability early exits can return `stop=True` without depositing
planning artifacts. Preserved terminal workspaces can then publish a status
change before the served artifact directory contains plan/conformance files.

## Requirements

- Verify the reported fix-pass worktree guard exits in
  `src/awf/control/executor/execution_validation.py`.
- Add a focused regression test proving fix-pass worktree guard stops deposit
  planning artifacts before returning.
- Make the smallest implementation change so all validation fix-pass
  `_ensure_worktree_available(...)` stop paths preserve planning artifacts.
- Run only targeted validation for the changed behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Add a regression in the existing fix-cycle unit-test module for a
   `validation_fix_agent_run` worktree guard returning `False`.
2. Confirm the regression fails before the production change.
3. Add planning artifact deposits immediately before the affected fix-pass
   worktree-availability `stop=True` returns.
4. Re-run the focused regression/module subset.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_009.py -q`

Full AWF/GitHub validation remains managed by AWF after this agent phase.
