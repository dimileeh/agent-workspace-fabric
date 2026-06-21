# PRRT_kwDOSJAM6s6K05EK Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K05EK_PLAN.md`

## Requirement Status

- Recovered missing-HEAD protected-scope violations must carry an existing
  `_PROTECTED_SCOPE_*` reason code: Complete. The recovered protected-scope
  violation branch now raises `_MonitorPolicyBlockedError` with
  `_PROTECTED_SCOPE_REPAIR_FAILED_REASON`.
- `_GitPushResult` handlers for monitor policy exceptions must preserve the
  exception's reason code: Complete. Comment, CI, and sync-base repair mappings
  now use `exc.reason_code`.
- Generic `_MonitorPolicyBlockedError` instances must continue to default to
  `MONITOR_POLICY_BLOCKED`: Complete. The exception defaults to
  `_MONITOR_POLICY_BLOCKED_REASON`, and existing generic policy tests still pass.
- Add focused tests for the reason-code preservation and protected-scope result
  classification: Complete. Added a direct exception/result contract test and a
  missing-HEAD recovered protected-scope regression.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/types.py`
- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `src/awf/runtime/pr_monitor_runner/fix_cycle.py`
- `src/awf/runtime/pr_monitor_runner/ci_ops.py`
- `tests/unit/runtime/test_pr_monitor_remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Focused commands run:

- Red: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q -k "policy_blocked_error or protected_scope"` failed before implementation because `_MonitorPolicyBlockedError` did not accept a `reason_code`.
- Red: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k "missing_head_recovery_blocks_recovered_protected_scope"` failed before implementation because `_MonitorPolicyBlockedError` had no `reason_code`.
- Green: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q -k "policy_blocked_error or protected_scope"` passed.
- Green: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k "missing_head_recovery_blocks_recovered_protected_scope"` passed.
- Generic policy regressions: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_002.py -q -k "policy_blocked"` passed.
- Generic policy regressions: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py -q -k "policy_blocked"` passed.
- Generic policy regressions: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k "policy"` passed.
- Lint: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/types.py src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py` passed.

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run in this workspace phase.
