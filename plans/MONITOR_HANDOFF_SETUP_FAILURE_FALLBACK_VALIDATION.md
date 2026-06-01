# Monitor Handoff Setup Failure Fallback Validation

Plan reference: `MONITOR_HANDOFF_SETUP_FAILURE_FALLBACK_PLAN.md`

## Requirement Status

- Preserve the setup failure reason, message, reason code, and details when the
  normal `_mark_failed` handoff path succeeds: Complete. The primary
  `_mark_failed` path is unchanged.
- If the final relayed `_mark_failed` raises and the workspace is still
  `running`, persist a terminal `failed` transition through the repository using
  the original setup failure payload: Complete. The new fallback uses
  `WorkspaceRepository.transition_if_current` and records failure fields.
- Do not start the PR monitor after setup failure: Complete. Regression asserts
  no monitor run occurs.
- Respect stale/non-running workspaces by not forcing a terminal state over a
  newer status: Complete. The fallback is guarded by `from_status=running`.
- Add a focused regression test for the setup-failure path where all normal
  `_mark_failed` attempts raise: Complete.

## Evidence

Files changed:

- `src/awf/control/executor/monitor_handoff.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
- `plans/MONITOR_HANDOFF_SETUP_FAILURE_FALLBACK_PLAN.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  - Passed: 19 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py`
  - Passed.

Full AWF/GitHub validation was not run during the agent phase; AWF owns the
broad validation suite, provenance, logs, timeouts, and merge gating after
agent completion.
