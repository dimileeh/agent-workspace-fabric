# Workflow Scope Review Comment 4585067239 Plan

## Problem Statement and Scope

Address the PR review-level feedback for comment `issue:4585067239` in the PR monitor repair path. The scope is limited to workflow-scope push failure handling, workflow-scope push result typing, and secondary owned-path prompt loading fallbacks.

## Requirements Checklist

- Requeue captured inline `defer` thread state when a `GITHUB_WORKFLOW_SCOPE_REQUIRED` push failure prevents the queued GitHub thread resolution from running.
- Preserve durable deferred-issue markers so requeued defers can retry resolution without filing duplicate tracking issues.
- Keep workflow-scope `fix_committed` items marked `needs_human` with the permission reason, and keep false-positive review-comment resolution behavior unchanged.
- Normalize workflow-scope push result `stdout` to a string even if a runner returns `None`.
- Keep the strict `_owned_paths_for_prompt` helper behavior for direct DB contract failures, while making secondary prompt call sites fall back to no owned paths when owned-path loading fails.
- Do not edit protected workflow/configuration files, switch branches, push, rebase, or run broad AWF/GitHub validation.

## Implementation Steps

1. Update focused regression tests first:
   - Change workflow-scope captured-defer tests to expect requeued state with durable marker preservation.
   - Add/adjust a helper-level test covering workflow-scope requeue cleanup for deferred threads.
   - Add a workflow-scope push regression for `stdout=None`.
   - Add a prompt fallback regression for direct `_address_thread` callers with a broken session factory.
2. Run the focused tests and confirm the new expectations fail against the current implementation.
3. Implement the smallest code changes:
   - Add captured defers to the workflow-scope resolution-dependent queue.
   - Update the workflow-scope requeue helper documentation/comments for defers.
   - Use `r.stdout or ""` in the workflow-scope push result.
   - Add a safe owned-path prompt fallback wrapper and use it in direct prompt call sites and CI-fix prompt construction.
4. Re-run only targeted tests for the changed behavior.
5. Create a validation document with requirement status and focused evidence. Note that broad AWF/GitHub validation is handled after agent completion.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes focused workflow-scope regressions.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py::test_owned_paths_for_prompt_propagates_session_factory_type_error -q`
  - Confirms the strict owned-path helper still propagates direct contract failures.
