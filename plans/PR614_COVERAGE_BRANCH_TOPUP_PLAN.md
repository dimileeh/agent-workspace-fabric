# PR614 Coverage Branch Top-Up Plan

## Problem Statement and Scope

PR #614 has a failing `python-full-coverage` CI check. The latest completed
failed run (`27842054721`) shows all Python shards passed, but the combined
coverage gate reported `98.95%`, below the required `99.00%`.

The downloaded `full-coverage-report` artifact shows the shortfall is driven by
uncovered branch outcomes in PR-monitor recovery code, including
`runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`.

Scope is limited to focused behavioral tests for already-implemented
pre-push-validation fix-pass error paths. No workflow, quality gate, or broad
configuration files will be changed.

## Requirements Checklist

- Add behavior-focused regression coverage for uncovered fix-pass branches.
- Verify rollback-failure precedence is asserted for recovered delta,
  recovered protected-diff, and recovered protected-scope violation paths.
- Verify agent runtime ownership repair failure short-circuits the fix pass.
- Run only focused local tests for the changed test file.
- Do not run full AWF/GitHub-owned coverage or broad CI-equivalent validation.

## Implementation Steps

1. Extend the existing recovered-head fix-pass test helper with a controlled
   agent runtime ownership repair result.
2. Add focused tests asserting rollback failure reasons override original
   recovered-head failure reasons where the implementation intentionally does so.
3. Add a focused test asserting agent runtime ownership repair failure returns
   `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED`.
4. Run the narrow pytest target for the edited test file.

## Assumptions/Changes

- After initial focused coverage work, the active current-HEAD CI run failed
  `python-coverage-shards (8)` on
  `test_first_party_code_files_stay_under_line_limit`: `execution_flow.py` has
  1522 lines, above the 1500-line guard. Add a behavior-neutral line-count
  reduction in that file and run the focused maintainability test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass_parts/test_pr_monitor_pre_push_validation_fix_pass_part_007.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`

Pass criteria: the focused test file passes locally, and the line-limit guard
passes locally. Full coverage validation is left to AWF/GitHub CI after this
agent phase.
