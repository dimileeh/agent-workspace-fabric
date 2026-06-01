# PR 348 Focused CI Failures Plan

## Problem Statement and Scope

PR #348 is failing the Python full coverage job on four focused unit tests. The
local focused repro confirms:

- monitor handoff setup failure paths preserve the setup dependency reason code
  but make one fewer `_mark_failed` attempt than the regression tests require;
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py` exceeds the
  first-party file line limit.

Scope is limited to fixing those focused CI failures without weakening checks,
changing protected workflow/configuration files, pushing, switching branches, or
running broad AWF/GitHub-owned validation locally.

## Requirements Checklist

- Preserve setup dependency network failure reason codes, messages, and details
  across monitor handoff setup persistence failures.
- Make the terminal monitor handoff setup fallback exercise the normal
  `_mark_failed` path once more before direct database persistence.
- Keep PR monitor pre-push validation tests below the first-party file line
  limit by splitting test coverage without changing behavior.
- Run only focused tests/checks for the changed behavior.
- Record validation evidence in `plans/CI_348_FOCUSED_FAILURES_VALIDATION.md`.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Update the monitor handoff setup failure handler so wrapper persistence is
   retried once before direct terminal fallback.
2. Split the oversized pre-push validation test module by moving the final mixed
   127 fix-pass regression tests into a small companion module that imports the
   shared test helpers.
3. Run the provided focused repro command until it passes.
4. Run the moved companion test module if needed to prove collection/imports.
5. Write validation results against this plan.
6. Commit the scoped changes locally.

## Verification Commands and Pass Criteria

Focused repro:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_final_mark_failed_error_terminal_fallback tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_release_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Pass criteria: all selected tests pass.

Additional focused test for split module, if needed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py -q
```

Pass criteria: all moved tests pass. Full AWF/GitHub validation remains managed
by AWF after agent completion.
