# PRRT_kwDOSJAM6s6K05EK Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6K05EK` reports that missing-HEAD recovery in
`remote_repair.py` detects recovered protected-scope violations but raises a
generic `_MonitorPolicyBlockedError`. Callers then surface either the default
git-push failure or `MONITOR_POLICY_BLOCKED`, so `_GitPushResult` does not enter
the protected-scope blocked/terminal handling.

Scope is limited to preserving a protected-scope reason code for this recovered
violation path and focused regression coverage. Generic monitor policy blocks
must keep their existing `MONITOR_POLICY_BLOCKED` behavior.

## Requirements Checklist

- Recovered missing-HEAD protected-scope violations must carry an existing
  `_PROTECTED_SCOPE_*` reason code.
- `_GitPushResult` handlers for monitor policy exceptions must preserve the
  exception's reason code.
- Generic `_MonitorPolicyBlockedError` instances must continue to default to
  `MONITOR_POLICY_BLOCKED`.
- Add focused tests for the reason-code preservation and protected-scope result
  classification.

## Implementation Steps

1. Add an optional `reason_code` to `_MonitorPolicyBlockedError`, defaulting to
   `_MONITOR_POLICY_BLOCKED_REASON`.
2. Raise the recovered protected-scope violation error with
   `_PROTECTED_SCOPE_REPAIR_FAILED_REASON`.
3. Update narrow caller mappings that build `_GitPushResult` from
   `_MonitorPolicyBlockedError` to use `exc.reason_code`.
4. Add focused unit tests that fail before the implementation and pass after it.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q -k "policy_blocked_error or protected_scope"`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q -k "missing_head_recovery_blocks_recovered_protected_scope"`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/types.py src/awf/runtime/pr_monitor_runner/remote_repair.py src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/fix_cycle.py src/awf/runtime/pr_monitor_runner/ci_ops.py tests/unit/runtime/test_pr_monitor_remote_ops.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
