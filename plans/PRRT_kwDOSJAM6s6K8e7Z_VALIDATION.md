# PRRT_kwDOSJAM6s6K8e7Z Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K8e7Z_PLAN.md`

## Requirement Status

- Preserve `_MonitorPolicyBlockedError.reason_code` when recovered HEAD
  filesystem recovery is policy-blocked: Complete.
- Keep cleanup behavior for policy-blocked missing-HEAD recovery unchanged:
  Complete.
- Add/update a focused unit regression for the preserved reason code: Complete.
- Run only focused local validation; full AWF/GitHub validation remains managed
  by AWF after agent completion: Complete.

## Evidence

- Changed `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` to log and
  return `exc.reason_code` for policy-blocked recovered HEAD filesystem
  recovery.
- Updated
  `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py` so the
  existing cleanup regression raises `_PROTECTED_SCOPE_REPAIR_FAILED_REASON`
  and expects that reason in the result.
- Confirmed the updated regression failed before the production change with
  `MONITOR_POLICY_BLOCKED` returned instead of `PROTECTED_SCOPE_REPAIR_FAILED`.
- Focused validation passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k missing_head_recovery_policy_block_cleans_residue`
- Focused lint passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`

Full AWF/GitHub validation was not run locally because AWF owns broad
validation, provenance, logs, timeouts, and merge gating after agent completion.
