# PR 348 Focused CI Failures Validation

Plan reference: `plans/CI_348_FOCUSED_FAILURES_PLAN.md`

## Requirement Status

- Complete: Preserve setup dependency network failure reason codes, messages,
  and details across monitor handoff setup persistence failures.
- Complete: Make the terminal monitor handoff setup fallback exercise the normal
  `_mark_failed` path once more before direct database persistence.
- Complete: Keep PR monitor pre-push validation tests below the first-party file
  line limit by splitting test coverage without changing behavior.
- Complete: Run only focused tests/checks for the changed behavior.
- Complete: Record validation evidence in this document.
- Complete: Commit the fix locally with a conventional commit message.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py`
- `plans/CI_348_FOCUSED_FAILURES_PLAN.md`
- `plans/CI_348_FOCUSED_FAILURES_VALIDATION.md`

Focused commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_mark_failed_from_monitor_handoff_setup_failure_swallows_mark_failed_error tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_mark_failed_from_monitor_handoff_setup_failure_reraises_direct_fallback_error -q
```

Result: `2 passed in 1.77s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py -q
```

Result: `2 passed in 5.14s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_final_mark_failed_error_terminal_fallback tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_release_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q
```

Result: `4 passed in 7.12s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_mixed_127.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py
```

Result: `Success: no issues found in 1 source file`.

Full AWF/GitHub validation was not run locally; AWF owns the broad validation
suite, coverage gate, provenance, and merge gating after agent completion.

## Gaps

No planned requirements remain partial or missing.
