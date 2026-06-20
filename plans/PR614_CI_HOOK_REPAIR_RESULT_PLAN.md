# PR614 CI Hook Repair Result Plan

## Problem Statement And Scope

The PR review thread reports that `_run_ci_fix` raises
`_MonitorMirrorHooksPathRepairFailedError` when post-agent mirror hook repair
fails after a plumbing exception. That raise occurs before the later exception
handler that converts the monitor error into a structured `_GitPushResult`, so
the monitor operation can escape instead of failing the workspace with
`MIRROR_HOOKS_PATH_POISONED`.

Scope is limited to the CI repair path in
`src/awf/runtime/pr_monitor_runner/ci_ops.py` and its focused regression tests.

## Requirements Checklist

- Return a failed `_GitPushResult` with reason code
  `MIRROR_HOOKS_PATH_POISONED` when post-agent mirror hook repair fails in
  `_run_ci_fix`.
- Preserve the existing behavior where a post-agent plumbing exception is
  re-raised after successful mirror hook repair and commit-sink processing.
- Update focused regression coverage for the changed CI repair branch.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests for the
  changed behavior.

## Implementation Steps

1. Change the post-agent hook-repair failure branch to return the structured
   failed push result instead of raising before the commit-sink try block.
2. Update the existing regression test that currently expects the escaped
   exception to assert the structured result instead.
3. Run the narrow unit test that covers this branch.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k cleanup_error_repairs_hooks_path`
  - Passes, proving both successful repair propagation and failed repair
    structured result behavior.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
