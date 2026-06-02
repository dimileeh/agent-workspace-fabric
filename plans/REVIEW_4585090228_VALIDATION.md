# Review 4585090228 Validation

Plan reference: `plans/REVIEW_4585090228_PLAN.md`

## Requirement Status

- Complete: Clarified failed/null-runtime reservation polarity without changing
  release-gate safety semantics.
- Complete: Planning-scope auto-retry now uses the normal source-runtime gate
  instead of creating a known-doomed port-bearing retry row while the source
  runtime is unreleased.
- Complete: Unreleased source runtime records an operator-visible
  `workspace.planning_scope_auto_retry_blocked` event with retry guidance.
- Complete: Focused tests were updated for the changed planning-scope auto-retry
  call and blocked event.
- Complete: Broad AWF/GitHub-owned validation was not run during the agent
  phase.

## Evidence

Files changed:

- `src/awf/control/executor/planning_ops.py`
- `src/awf/service/workspaces_retry.py`
- `tests/unit/control/test_executor_parts/test_executor_part_003.py`
- `tests/unit/control/test_executor_planning_auto_retry_transactions.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `plans/REVIEW_4585090228_PLAN.md`
- `plans/REVIEW_4585090228_VALIDATION.md`

Focused failing-first check:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_003.py::TestHappyPathPart002::test_planning_profile_fails_when_plan_phase_changes_code tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_blocks_on_unreleased_source_runtime -q`
  - Failed before implementation with `2 failed`.
  - The executor-path regression saw `ignore_source_runtime_check=True` where
    the updated expectation requires the normal runtime gate.
  - The new blocked-event regression failed because the current helper still
    passed `ignore_source_runtime_check=True`.

Focused passing checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_003.py::TestHappyPathPart002::test_planning_profile_fails_when_plan_phase_changes_code tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_blocks_on_unreleased_source_runtime -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_legacy_null_runtime_source_without_reservation tests/unit/service/test_workspace_retry_port.py::test_retry_allows_when_source_compose_project_name_is_none -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_rolls_back_before_failed_event tests/unit/control/test_executor_planning_auto_retry_transactions.py::test_auto_retry_planning_scope_failure_blocks_on_unreleased_source_runtime -q`
  - Passed: `2 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_runtime_gate_override_excludes_source_from_port_conflict tests/unit/service/test_workspace_retry_port.py::test_retry_runtime_gate_override_succeeds_when_source_runtime_released tests/unit/service/test_workspace_retry_port.py::test_retry_runtime_gate_override_succeeds_no_host_ports_runtime_not_released -q`
  - Passed: `3 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py src/awf/control/executor/planning_ops.py tests/unit/control/test_executor_parts/test_executor_part_003.py tests/unit/control/test_executor_planning_auto_retry_transactions.py tests/unit/service/test_workspace_retry_port.py`
  - Passed.

Full AWF/GitHub validation was not run during the agent phase per the workspace
contract; AWF owns broad validation, provenance, logs, and merge gating after
completion.

## Gaps

None.
