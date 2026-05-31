# Operator Hint Review 4585104324 Validation

Plan reference: `plans/OPERATOR_HINT_REVIEW_4585104324_PLAN.md`

## Requirement Status

- Complete: Preserve the existing operator hint persistence behavior and
  regression coverage for non-pushed terminal hint statuses.
  - Evidence: Existing implementation in
    `src/awf/runtime/pr_monitor_runner/loop.py` persists terminal
    `needs_human`/`agent_failed` non-pushed hint statuses before returning.
  - Evidence: Focused regression
    `tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_non_pushed_terminal_status_is_persisted_before_return`
    passed.

- Complete: `_control_response` and operation-result warning replay use
  `WorkspaceControlWarningResponse` values at the response boundary.
  - Evidence: `src/awf/service/controls_helpers.py` now types `_control_response`
    warnings as `list[WorkspaceControlWarningResponse]` and validates stored
    operation-result warning payloads into that schema.
  - Evidence: `tests/unit/service/test_controls_helpers.py` now asserts replayed
    warnings are schema objects.

- Complete: Remonitor warning payloads remain JSON-serializable in operation
  results and workspace events.
  - Evidence: `src/awf/service/controls.py` stores remonitor warnings as typed
    schema values in memory and serializes them through
    `_control_warning_payloads` before writing event or operation payloads.
  - Evidence: Focused remonitor warning tests in
    `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
    passed for no-reason past-settle, current-candidate-head, failed-workspace
    past-settle, and stale-last-SHA elapsed-marker cases.

- Complete: Remonitor `requested_at` guard failures raise a clear runtime error
  even when Python assertions are disabled.
  - Evidence: `src/awf/service/controls.py` replaced both
    `assert requested_at is not None` guards with
    `_require_operator_remonitor_requested_at`.
  - Evidence:
    `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py::test_remonitor_requested_at_invariant_raises_clear_error`
    forces the timestamp invariant failure and verifies the clear
    `RuntimeError`.

- Complete: Concurrent-hint supersession in the lifecycle merge helper is
  documented where the replacement occurs.
  - Evidence: `src/awf/runtime/pr_monitor_runner/lifecycle.py` now documents the
    branch where a newer DB hint supersedes an in-flight hint only in persisted
    state.

- Complete: Run only focused validation commands for the touched behavior.
  - Evidence: Ran:
    `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_helpers.py tests/unit/runtime/test_pr_monitor_operator_hints.py::test_operator_hint_non_pushed_terminal_status_is_persisted_before_return tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py::test_remonitor_requested_at_invariant_raises_clear_error tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py::test_remonitor_no_reason_past_settle_warns_without_operator_hint tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py::test_remonitor_no_reason_past_settle_arms_current_candidate_head tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py::test_remonitor_failed_workspace_past_settle_persists_operator_hint_and_warns tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py::test_remonitor_failed_workspace_past_settle_uses_elapsed_marker_when_last_sha_stale -q`
  - Evidence: Ran:
    `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py src/awf/service/controls_helpers.py src/awf/runtime/pr_monitor_runner/lifecycle.py`
  - Evidence: Ran:
    `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py src/awf/service/controls_helpers.py src/awf/runtime/pr_monitor_runner/lifecycle.py tests/unit/service/test_controls_helpers.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`

## Results

- Focused pytest: passed (`9 passed`).
- Focused mypy: passed (`Success: no issues found in 3 source files`).
- Focused ruff: passed (`All checks passed!`).

Full AWF/GitHub validation was not executed inside the agent phase; AWF owns the
broader validation, provenance, logs, and merge gating after completion.
