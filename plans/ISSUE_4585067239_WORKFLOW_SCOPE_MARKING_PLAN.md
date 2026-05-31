# Issue 4585067239 Workflow Scope Marking Plan

## Problem Statement And Scope

Greptile reported that a workflow-scope GitHub push rejection rewrites every
`publish_dependent_ids` entry to `needs_human`, even when the agent already
classified some entries as `false_positive` or captured `defer`. The repair
should only mark unpublished committed fixes as `needs_human` for the workflow
permission blocker.

The same review summary mentioned a silent `TypeError` fallback in
`_owned_paths_for_prompt`. Current branch code already propagates the
`TypeError`, with an existing regression in
`tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py`.
No code change is planned for that stale portion.

## Requirements Checklist

- Add a regression covering mixed `false_positive` and `fix_committed` review
  threads when GitHub rejects a workflow-file push for missing `workflow` scope.
- Ensure the workflow-scope handler marks only current `fix_committed` items as
  `needs_human`.
- Preserve existing rollback behavior for non-workflow push failures.
- Preserve `false_positive` and captured `defer` verdicts on workflow-scope
  rejection.
- Keep validation focused; AWF/GitHub own broad validation after this agent
  phase.

## Implementation Steps

1. Add a failing unit test in the existing PR-monitor part 006 regression file
   for a mixed-verdict workflow-scope rejection.
2. Update `_mark_publish_dependent_items_needs_human` to skip items whose
   current verdict is not `fix_committed`.
3. Run the focused test(s) for the changed PR-monitor regression file.
4. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes with the new mixed-verdict regression.

Full AWF/GitHub validation is intentionally not run locally in this agent phase.
