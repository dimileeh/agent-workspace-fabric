# Workflow-Scope Comment Fix Retry Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6F8Znx` reports that comment-repair pushes rejected for missing GitHub `workflow` scope are stored as `needs_human` for `fix_committed` items. That leaves the unresolved review item marked handled, so after an operator grants a workflow-capable token the next monitor pass does not retry the local fix push.

Scope is limited to PR monitor comment-repair state handling and focused unit coverage around workflow-scope push failures. No protected workflow/configuration files are edited.

## Requirements Checklist

- Keep workflow-scope push failures visible through the existing operation failure and best-effort human notification.
- Requeue `fix_committed` inline threads and review comments after a workflow-scope push rejection so the next monitor decision can retry.
- Continue clearing inline resolution-dependent verdicts such as `false_positive` and captured `defer` when their GitHub resolution could not run.
- Preserve durable review-level `false_positive` resolution state.
- Add/update focused regression tests before implementation and run only targeted tests.

## Implementation Steps

1. Update workflow-scope regression expectations to require cleared, retryable state for publish-dependent `fix_committed` items.
2. Confirm the updated focused test fails against the current helper.
3. Change `_requeue_workflow_scope_publish_dependent_items` so publish-dependent fixes are cleared instead of converted to `needs_human`.
4. Update comments/docstrings to describe retryable publish-dependent fixes.
5. Run focused tests covering the helper and comment-repair workflow-scope execution path.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k workflow_scope`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_010.py -q -k workflow_scope`

Pass criteria: targeted tests pass, and validation notes explicitly state that full AWF/GitHub validation is managed after agent completion.
