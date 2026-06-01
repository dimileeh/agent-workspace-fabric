# Address Review Comment 4590903660 Preflight Notify Plan

## Problem Statement And Scope

Greptile identified that merge-method preflight GitHub errors use the
transient retry helper but terminate the workspace when the helper returns
`False`. That covers non-transient errors such as 401/403 and leaves the
operator with a failed workspace instead of the existing merge-blocker behavior:
post a human notification and keep the monitor alive for intervention.

Scope is limited to the merge-method preflight fallback in
`src/awf/runtime/pr_monitor_runner/merge_loop.py`, a focused regression in
`tests/unit/runtime/test_pr_monitor_merge_methods.py`, and this plan/validation
pair. No protected workflow, quality-gate, or broad validation configuration
files will be edited.

## Requirements Checklist

- Add a regression showing a non-transient merge-method preflight error posts a
  human notification instead of terminating.
- Keep transient preflight errors retrying without notification.
- Preserve existing merge-method blocker state behavior for permanent method
  mismatches.
- Run focused validation only; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add a merge-method unit test for a branch-rules preflight 403 that expects
   no merge call, one human notification, one poll sleep, and no terminal
   workspace failure.
2. Change the `merge_method_preflight_error` fallback after transient retry
   handling to mirror the merge-blocker path: log, post a human notification,
   sleep for the poll interval, and return `False`.
3. Run the focused merge-method test file and focused ruff on touched files.
4. Record validation evidence in
   `ADDRESS_REVIEW_4590903660_PREFLIGHT_NOTIFY_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_merge_methods.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_pr_monitor_merge_methods.py`
  passes.
- Full AWF/GitHub validation is not run locally per workspace contract.
