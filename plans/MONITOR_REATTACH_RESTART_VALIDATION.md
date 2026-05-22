# Monitor Reattach Restart Validation

Plan reference: `plans/MONITOR_REATTACH_RESTART_PLAN.md`

## Requirement Status

- Recover `monitoring_pr` workspaces with open PRs even when old runtime is
  still `running`:
  Complete. `ControlWorker._recover_stale_active_execution()` now records a
  recoverable remonitor finding for this state instead of falling into generic
  stale-active failure.
- Clear stale claims and redispatch monitor:
  Complete. The existing recoverable runtime-stranding path clears the stale
  claim, records `workspace.runtime_stranded_detected`, and allows normal
  monitor claiming/resume in the same `run_once`.
- Preserve active execution failure behavior:
  Complete. The change is gated to `WorkspaceStatus.monitoring_pr` plus an open
  PR URL and does not alter `running`/`validating`/`pushing` behavior.
- Add regression coverage:
  Complete. `test_monitoring_pr_running_runtime_after_restart_remonitors_open_pr`
  covers the exact failure mode: a running monitor runtime, expired monitor
  claim, and an existing stale-active event.
- Recover `ws_cdd335704e12498ca87be8d4`:
  Complete. Operator remonitor succeeded and the worker resumed PR #280.

## Evidence

- Focused worker tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k 'monitoring_pr_running_runtime_after_restart or monitoring_pr_runtime_stranding_clears_expired_claim' -q`
  passed: `2 passed`.
- Lint/format:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
  `uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py tests/unit/control/test_worker.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
  passed.
- Live recovery:
  `awf workspace remonitor ws_cdd335704e12498ca87be8d4` succeeded. Worker logs
  show `executor.resume_pr_monitor` for PR #280, then
  `monitor.action action=AddressComments ... head_sha=75c385161d`, then
  `agent.run.start agent=codex model=gpt-5.3-codex-spark`.

## Notes

The original failure happened because the restart recovery path treated a
healthy but detached PR-monitor runtime as a lost active execution. PR monitors
have a durable recovery point, the open PR, so they should remonitor rather than
fail solely because the old compose runtime is still running.
