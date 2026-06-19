# PRRT_kwDOSJAM6s6KXLaI preserve commit-sink failures when recording provider outages validation

## Result
Implemented the fix from
`plans/PRRT_kwDOSJAM6s6KXLaI_COMMIT_SINK_PROVIDER_STATE_PLAN.md` for the
second review comment on thread `PRRT_kwDOSJAM6s6KXLaI` (discussion
r3431603601). The three commit-sink `except` handlers in
`_run_ci_fix` now record the provider recovery state via
`_handle_provider_agent_run_error` while suppressing the recovery
control-flow exceptions
(`ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
`ProviderRecoveryAuthError`) so the commit-sink failure result is
returned to the loop and the operator sees the specific reason code
(protected-scope diff unavailable / ownership repair failed / policy
blocked), not `PROVIDER_OUTAGE`.

## Root-cause coverage
- Review thread `PRRT_kwDOSJAM6s6KXLaI` (discussion r3431603601): the
  reviewer observed that awaiting `_handle_provider_agent_run_error`
  inside the commit-sink `except` handlers in `ci_ops.py:120-150`
  raised a recovery control-flow exception that made the commit-sink
  failure `return` unreachable, so the loop surfaced
  `PROVIDER_OUTAGE` instead of the specific commit-sink reason. Worse,
  when `_commit_dirty_worktree` raised before committing, the dirty
  tree was left behind and the next attempt tripped the pre-existing
  dirty guard instead of surfacing the commit-sink failure.
- Verified the claim against the actual code:
  `_handle_provider_agent_run_error` (`provider_ops.py:288`) calls
  `_persist_state` and `_record_provider_agent_run_error` (the
  recording side-effects) and then raises
  `ProviderRecoveryRetryError` / `ProviderRecoveryFallbackError` /
  `ProviderRecoveryAuthError` for recoverable errors. The recording
  side-effects complete before the raise, so suppressing only the
  control-flow raise preserves the provider recovery state while
  letting the commit-sink failure result win.
- The existing regression test
  `test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return`
  (part_005:662) covered only the deterministic/non-raising handler
  path (PRRT_kwDOSJAM6s6KUiEG) and did NOT cover the recoverable
  provider error case this comment points at.

## Fix
- `src/awf/runtime/pr_monitor_runner/ci_ops.py`: in each of the three
  commit-sink `except` handlers, wrapped the
  `_handle_provider_agent_run_error` call with
  `contextlib.suppress(ProviderRecoveryRetryError,
  ProviderRecoveryFallbackError, ProviderRecoveryAuthError)`.
  `contextlib.suppress` matches the established codebase convention
  (used in `service/usage_collection.py`, `node/compose_manager.py`,
  `control/worker/claims.py`, etc.).
- Added `import contextlib` and imported the three recovery control-flow
  exception types.

## Verification (focused only — broad validation owned by AWF/GitHub)
- New regression test
  `test_ci_fix_preserves_commit_sink_failure_when_provider_recovers`
  (parametrized over the three commit-sink exceptions):
  - TDD red on unfixed code: `ProviderRecoveryRetryError` propagated
    and clobbered the commit-sink failure result (3 failed).
  - TDD green on fixed code: commit-sink failure `_GitPushResult`
    returned with the specific reason code; provider recovery state
    still recorded (handler invoked once with the stored
    `AgentRunError`).
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py -q`
  - Passed: `26 passed` (includes the new parametrized regression test
    and the existing
    `test_ci_fix_records_provider_agent_run_error_before_commit_sink_early_return`
    — the deterministic/non-raising handler path is unchanged).
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_parts/ tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs.py tests/unit/runtime/test_pr_monitor_pre_push_validation_repairs_validated_push.py tests/unit/runtime/test_pr_monitor_task_tag_threading.py -q`
  - Passed: `244 passed` (no regression in the repair callers, pre-push
    validation, or sync_base surfaces that route through `_run_ci_fix`).
- `uv run --python 3.12 --extra dev python -m pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/ -q`
  - Passed: `257 passed` (no regression in the closely related
    coverage-edge regression suites, including the prior
    PRRT_kwDOSJAM6s6KXLaI operation-owned scoping tests).
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
  - Passed (no lint errors; unused imports removed; `contextlib.suppress`
    convention followed).
- `uv run --python 3.12 --extra dev ruff format --check <same files>`
  - Passed (2 files already formatted).
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/ci_ops.py`
  - Passed (no issues in 1 source file).

## Notes
- Full AWF/GitHub broad validation (full coverage gate, whole-repo
  suites, frontend builds) is managed by AWF after agent completion
  per the workspace contract; only the focused checks above were run
  in-agent.
- The fix is scoped to the three commit-sink handlers in `_run_ci_fix`
  only. The success-path `_handle_provider_agent_run_error` call at
  `ci_ops.py:152` is intentionally left as-is: there is no commit-sink
  failure to preserve there, so the recovery control-flow exception
  correctly propagates to the loop (matching `comments.py:285` and
  `remote_ops.py:754`).
