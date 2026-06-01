# PRRT_kwDOSJAM6s6F9d27 Pre-Push Failure Cache Plan

## Problem Statement and Scope

The PR monitor pre-push validation path computes failed validation commands more
than once while deriving the validation reason code and the toolchain-missing
reason. The review thread asks for caching so both decisions use the same
failure scan.

Scope is limited to `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
and its focused unit tests.

## Requirements Checklist

- Add a regression test that fails when one pre-push validation call traverses
  failed commands more than once for a pure command-not-found result.
- Reuse one failed-command tuple while deriving the preferred failure,
  toolchain-missing failure, persisted validation reason, and pre-push reason.
- Preserve existing mixed-failure and coverage-failure reason-code behavior.
- Do not run broad AWF/GitHub validation; record focused local checks only.

## Implementation Steps

1. Add the focused failing regression to
   `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`.
2. Run that single test to confirm the duplicate traversal failure.
3. Refactor pre-push validation helper functions so cached failures are passed
   through the preferred-failure and toolchain-missing decisions.
4. Run the focused regression plus nearby pre-push validation tests that cover
   toolchain, mixed failure, and coverage reason codes.
5. Write validation notes in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -k failed_commands_once -q`
  should fail before the fix and pass after the fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -k "failed_commands_once or toolchain_missing_bypasses_fix_pass or mixed_127_prefers_real_failure_for_fix_pass or coverage_failure_persists_coverage_reason_code" -q`
  should pass after the fix.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
