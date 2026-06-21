# Baseline Mirror Hooks Repair Plan

## Problem Statement And Scope

An inline PR review reports that baseline coverage cleanup/plumbing failures can bypass
mirror `core.hooksPath` repair before control reaches the existing pre-agent guard.
The scope is limited to executor mirror-hook repair around baseline coverage
measurement and a focused regression test.

## Requirements Checklist

- Add mirror hooks repair when `_measure_and_persist_baseline_coverage` raises a
  `ComposeExecCleanupError`.
- Preserve the existing cleanup failure handling and reason code emitted by the
  outer executor failure handler.
- Do not change unrelated executor stages or broad validation behavior.
- Add a focused regression test for the baseline coverage cleanup-error path.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_executor_mirror_hooks_path.py`
   that simulates a baseline coverage `ComposeExecCleanupError` and asserts the mirror
   repair is attempted before the executor marks the workspace failed.
2. Wrap the baseline coverage measurement call in
   `src/awf/control/executor/execution_flow.py` with the cleanup-error repair path.
3. Run the targeted unit test file or focused test selection only.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  passes.
- Full AWF/GitHub validation is intentionally left to AWF after agent completion.
