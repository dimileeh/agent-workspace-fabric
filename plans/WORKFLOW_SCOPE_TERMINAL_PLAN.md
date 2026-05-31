# Workflow Scope Terminal Plan

## Problem Statement And Scope

GitHub rejects pushes that modify workflow files when the monitor token lacks
the `workflow` scope. AWF already parses that rejection as
`GITHUB_WORKFLOW_SCOPE_REQUIRED`, but CI-repair and sync-base monitor actions
currently rely on `_GitPushResult.terminal_monitor_failure` to stop retrying.
Because that property does not include workflow-scope failures, those actions
can retry a non-recoverable permission error.

Scope is limited to monitor-owned push failure handling and focused regression
coverage for CI-repair and sync-base actions.

## Requirements Checklist

- Add regression coverage showing sync-base stops terminally on
  `GITHUB_WORKFLOW_SCOPE_REQUIRED`.
- Add regression coverage showing CI-repair stops terminally on
  `GITHUB_WORKFLOW_SCOPE_REQUIRED`.
- Preserve existing workflow-scope parsing and audit outcome labels.
- Avoid broad AWF/GitHub-owned validation; run focused tests only.

## Implementation Steps

1. Add focused failing tests for sync-base and CI-repair workflow-scope push
   rejection behavior.
2. Update `_GitPushResult.terminal_monitor_failure` to include
   `workflow_scope_required`.
3. Run the focused tests that cover the changed behavior.
4. Record validation evidence in `plans/WORKFLOW_SCOPE_TERMINAL_VALIDATION.md`.
