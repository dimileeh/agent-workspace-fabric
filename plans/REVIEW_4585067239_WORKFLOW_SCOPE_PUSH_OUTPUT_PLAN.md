# Review 4585067239 Workflow-Scope Push Output Plan

## Problem Statement And Scope

PR review comment `issue:4585067239` reports two workflow-scope push detection gaps in
`src/awf/runtime/pr_monitor_runner/remote_ops.py`:

- `_git_push_result` scans `r.stderr or r.stdout`, which misses workflow-scope
  rejection text when stderr has unrelated content and stdout carries the
  actionable GitHub rejection.
- Missed workflow-file push rejections are silent when output mentions
  `.github/workflows/` but does not match the current missing-scope regexes.

Scope is limited to the PR monitor git-push failure path and focused regressions
for that behavior. No workflow, quality-gate, or broad validation configuration
files will be edited.

## Requirements Checklist

- Add a failing regression proving workflow-scope detection scans combined
  stderr and stdout.
- Add a failing regression proving unmatched workflow-file push output logs an
  explicit warning before falling through to the generic failure path.
- Keep known workflow-scope detections mapped to
  `GITHUB_WORKFLOW_SCOPE_REQUIRED` with selective repair semantics unchanged.
- Keep generic push rejection/resync behavior unchanged for non-workflow output.
- Run only targeted tests for the changed behavior; full AWF/GitHub validation
  remains owned by AWF after agent completion.

## Implementation Steps

1. Extend the focused PR monitor unit tests in
   `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`.
2. Confirm the new tests fail against the current implementation.
3. Update `_git_push_result` to combine stderr and stdout before workflow-scope
   detection.
4. Add a small warning helper or inline warning for failed push output that
   mentions `.github/workflows/` but does not match workflow-scope patterns.
5. Run the focused tests and update validation evidence.
6. Commit the isolated fix locally with the required review-comment message.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Passes after implementation.
  - Initially fails for the new regressions before implementation, when practical.

Full suite, coverage, frontend build, OpenAPI drift, and CI-equivalent validation
are intentionally not run in the agent phase; AWF/GitHub own those gates after
completion.
