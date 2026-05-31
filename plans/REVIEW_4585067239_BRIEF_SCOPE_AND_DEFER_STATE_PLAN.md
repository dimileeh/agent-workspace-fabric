# Review 4585067239 Brief Scope and Defer State Plan

## Problem Statement and Scope

Greptile's review-level comment on PR #325 raises two follow-up concerns in the
workflow-scope comment-repair path:

- Short GitHub hook output such as `workflow permissions required` can miss
  `_workflow_scope_push_block` classification when no workflow path or
  workflow-file wording is present.
- Captured `defer` thread state should not be re-addressed after a
  workflow-scope push failure followed by an unrelated generic push failure.

Scope is limited to workflow-scope push-output classification and focused
regressions around comment-repair state. Protected workflow, quality-gate, and
repository configuration files are out of scope.

## Requirements Checklist

- Add a failing regression for a terse GitHub push hook message that says
  workflow permissions are required without naming `.github/workflows/`.
- Preserve the existing false-positive guard for unrelated workflow-scope text
  embedded in a generic remote rejection.
- Add a focused regression showing a captured `defer` thread is not re-addressed
  after a workflow-scope failure followed by a generic push failure in a later
  fix cycle.
- Keep existing workflow-scope wording variants and push-independent verdict
  preservation behavior intact.
- Run only targeted tests and focused lint for touched files; full AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Update `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
   with the terse hook-output regression and the two-cycle defer-state
   regression.
2. Run those focused tests first to confirm the detector regression fails before
   implementation where practical.
3. Relax `_workflow_scope_push_block` with a narrow direct-hook fallback that
   accepts terse workflow-scope-required output only when it also looks like git
   push output.
4. Re-run the focused tests plus nearby parser/state tests.
5. Record validation evidence in
   `plans/REVIEW_4585067239_BRIEF_SCOPE_AND_DEFER_STATE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py::<targeted tests>`
  - New detector test fails before the production change and passes after it.
  - Existing false-positive and defer-state tests continue passing.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  - Reports no lint errors.

Broad test suites, coverage gates, frontend builds, OpenAPI drift checks, and
CI-equivalent validation are intentionally left to AWF/GitHub after agent
completion.
