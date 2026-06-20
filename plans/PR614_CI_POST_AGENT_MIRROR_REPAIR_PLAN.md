# PR614 CI Post-Agent Mirror Repair Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6K9Pra` reports that `_run_ci_fix` logs a failed
post-agent mirror hooks-path repair, then re-raises the original adapter/plumbing
exception. If the repair fails, the shared mirror may remain poisoned, so the
monitor should fail closed with the mirror-hooks reason instead of masking it.

Scope is limited to the CI-fix post-agent repair path in
`src/awf/runtime/pr_monitor_runner/ci_ops.py` and the focused regression test
covering that path.

## Requirements Checklist

- Update the focused regression so a failed post-agent mirror hooks repair
  reports `MIRROR_HOOKS_PATH_POISONED`.
- Preserve the existing behavior when the post-agent mirror repair succeeds:
  re-raise the original adapter/plumbing exception.
- Keep the change minimal and avoid broad validation; AWF/GitHub own full
  validation after agent completion.

## Implementation Steps

1. Update the existing parametrized CI cleanup test to expect the mirror-hooks
   monitor failure only when the second repair call fails.
2. Run that focused test and confirm the old masking behavior fails the new
   expectation.
3. In `_run_ci_fix`, raise `_MonitorMirrorHooksPathRepairFailedError` from the
   failed post-agent repair exception.
4. Re-run the focused test.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -k ci_fix_cleanup_error_repairs_hooks_path -q`
  - Passes both parametrized cases.

Full AWF/GitHub validation is intentionally not run in the agent phase.
