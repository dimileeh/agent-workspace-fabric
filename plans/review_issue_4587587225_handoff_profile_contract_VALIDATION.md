# Review Issue 4587587225 Handoff Profile Contract Validation

Plan reference:
`plans/review_issue_4587587225_handoff_profile_contract_PLAN.md`

## Requirement Status

- Make the `profile` and `run_profile_setup` relationship explicit at the
  `_build_handoff_pr_monitor` boundary: Complete.
- Prevent future callers from silently re-running profile setup when they pass
  a pre-resolved handoff profile: Complete.
- Preserve current feature-PR and release-PR handoff behavior: Complete.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks:
  Complete.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `plans/review_issue_4587587225_handoff_profile_contract_PLAN.md`
- `plans/review_issue_4587587225_handoff_profile_contract_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_monitor_rejects_prepared_profile_with_setup_enabled -q
```

Result before implementation: failed because `_build_handoff_pr_monitor`
returned the monitor after invoking setup for a supplied profile.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_monitor_rejects_prepared_profile_with_setup_enabled -q
```

Result after implementation: passed, `1 passed in 0.75s`.

```bash
uv run --python 3.12 --extra dev ruff format src/awf/control/executor/monitor_handoff.py
```

Result after initial commit formatting check: reformatted one file.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_runs_profile_setup_before_monitor tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff::test_ahead_creates_release_pr_and_enters_monitoring -q
```

Result after implementation: passed, `2 passed in 3.14s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_handoff_monitor_rejects_prepared_profile_with_setup_enabled tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_runs_profile_setup_before_monitor tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py::TestSyncReleasePrHandoff::test_ahead_creates_release_pr_and_enters_monitoring -q
```

Result after formatting: passed, `3 passed in 3.15s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Result after implementation: passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.
