# Review 4396092575 Workflow Scope Signature Plan

## Problem Statement And Scope

PR review comment `4396092575` asks to tighten `_workflow_scope_push_block`
so callers do not handle a nullable tuple result. Scope is limited to the
workflow-scope push rejection helper, its immediate caller, focused tests, and
this plan/validation record.

## Requirements Checklist

- Replace the nullable `_workflow_scope_push_block` return type with a
  non-null structured result.
- Preserve existing behavior for GitHub missing-`workflow`-scope push
  detection, emitted reason code, stderr message, and path details.
- Do not edit protected workflow, quality-gate, or repository configuration
  files.
- Run focused validation only; broad AWF/GitHub validation remains owned by
  AWF after agent completion.

## Implementation Steps

1. Add a private result dataclass for parsed workflow-scope push blockers.
2. Update `_workflow_scope_push_block` to return that result for both matched
   and unmatched push output.
3. Update `_git_push_result` to branch on the result flag instead of checking
   for `None`.
4. Run the focused PR-monitor tests covering workflow-scope push handling and
   targeted lint/type checks for the touched Python surface.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_ops.py`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  passes.
- `git diff --check` passes.
