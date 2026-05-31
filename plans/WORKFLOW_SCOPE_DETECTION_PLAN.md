# Workflow Scope Detection Plan

## Problem Statement And Scope

PR #325 has a review-level follow-up noting that GitHub missing-`workflow`
scope push detection is too dependent on one message shape. The current monitor
path correctly treats detected workflow-scope push failures as
`GITHUB_WORKFLOW_SCOPE_REQUIRED`, but a nearby GitHub wording variation could
fall through to generic push failure handling.

Scope is limited to the PR monitor git-push detector and focused unit coverage.
No workflow, quality-gate, or configuration files are in scope.

## Requirements Checklist

- Add regression coverage for workflow-scope rejection wording that does not
  include the exact existing "without `workflow` scope" phrase.
- Preserve existing detection for the known GitHub "refusing to allow ..."
  message and workflow path extraction.
- Avoid broad false positives by requiring workflow-scope permission language
  plus push-rejection context or workflow-file context.
- Keep downstream result handling unchanged: detected failures still surface as
  `GITHUB_WORKFLOW_SCOPE_REQUIRED`.

## Implementation Steps

1. Add focused tests in the existing PR monitor regression file for alternate
   missing-workflow-scope push output shapes.
2. Run the targeted tests before implementation when practical to confirm the
   new regression fails.
3. Broaden `_workflow_scope_push_block` in
   `src/awf/runtime/pr_monitor_runner/remote_ops.py` with clearer helper
   predicates and multiple stable message patterns.
4. Re-run the focused PR monitor regression tests.
5. Record validation evidence in
   `plans/WORKFLOW_SCOPE_DETECTION_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes after implementation.
  - Fails before implementation for the new alternate-message regression.

Full AWF/GitHub validation is intentionally not run in this agent phase; AWF
owns broad validation and merge gating after agent completion.
