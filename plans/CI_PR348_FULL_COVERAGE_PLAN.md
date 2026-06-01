# CI PR 348 Full Coverage Plan

## Problem Statement and Scope

PR #348 fails the `python-full-coverage` CI job because the full test suite
passes but combined line+branch coverage reports `98.96%`, below the required
`99%`. The fix must add focused regression coverage for currently uncovered
behavior in the changed PR-monitor pre-push validation and monitor handoff setup
paths without weakening any quality gate.

## Requirements Checklist

- Preserve the 99% coverage gate and do not edit workflow or quality-gate
  configuration.
- Add focused tests for uncovered behavior in changed code paths rather than
  broad refactors.
- Cover monitor handoff setup cleanup-failure and best-effort event-recording
  error paths.
- Cover the extracted monitor handoff audit branch that emits an exhausted
  setup-dependency event without also emitting a retry event when retry count is
  zero.
- Cover pre-push validation helper branches for migration, coverage command,
  and coverage-provider failures without a command result.
- Run only focused local checks; AWF/GitHub owns broad coverage validation after
  agent completion.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Add focused unit tests to
   `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
   for monitor handoff setup cleanup failures, event recording exceptions, and
   setup-dependency audit event branching.
2. Add focused unit tests to
   `tests/unit/runtime/test_pr_monitor_pre_push_validation.py` for direct helper
   branches and coverage-provider failures with no command result.
3. Run the targeted tests for the touched files.
4. Run focused lint for the touched tests and source modules.
5. Record validation evidence in `plans/CI_PR348_FULL_COVERAGE_VALIDATION.md`.
6. Commit the scoped fix locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py -q`
  - Passes all focused unit tests for the touched behavior.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py src/awf/control/executor/monitor_handoff_setup.py src/awf/control/executor/monitor_handoff_audit.py src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  - Passes with no lint errors.

Full repository coverage is intentionally not run locally under the AWF
workspace contract; the GitHub/AWF post-agent validation owns that broad gate.
