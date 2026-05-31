# COMMENT_3329784173 Workflow Scope Fixtures Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F60V9` points out that
`_workflow_scope_push_block()` supports several missing-workflow-scope wording
variants, but the existing unit fixtures only cover a subset. The intended fix
is test-only coverage for the unexercised regex branches in
`src/awf/runtime/pr_monitor_runner/remote_ops.py`.

## Requirements Checklist

- Add unit-test stderr fixtures for missing/lacks/does-not-have/doesn't-have/has-no wording.
- Add unit-test stderr fixtures for requires/needs/must-include wording.
- Ensure every positive fixture includes workflow push context such as
  `.github/workflows/`, `create or update workflow`, or `remote rejected`.
- Preserve the existing negative test that unrelated workflow output is ignored.
- Do not change protected files, branch, push behavior, or broad AWF/GitHub validation.

## Implementation Steps

1. Extend the existing parametrized workflow-scope test in
   `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`.
2. Keep assertions aligned with current behavior: blocked result, workflow path extraction,
   and normalized message containing `` `workflow` scope``.
3. Run the narrow unit test covering `_workflow_scope_push_block()`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q -k workflow_scope_push_block`
  passes.
- Full AWF/GitHub validation remains managed by AWF after agent completion.
