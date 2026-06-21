# Recovered Pre-Push Committed Diff Plan

## Problem Statement and Scope

The missing-HEAD recovery path in PR monitor pre-push validation validates recovered commits through a synthetic dirty-status repair helper. Because recovery has already advanced `HEAD`, protected-file classification can compare the recovered commit to itself and miss validation-weakening committed changes.

Scope is limited to the recovered pre-push validation path in `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` and focused unit coverage for that path.

## Requirements Checklist

- Recovered commits must classify protected-file changes using the committed diff from `recovery_head..recovered`.
- Recovered commits with protected-scope violations must block before normal validation runs.
- Diff/classification failures in recovered committed-diff validation must produce the existing protected-scope-diff-unavailable failure.
- Keep the existing agent-runtime ownership repair behavior for recovered changed paths.
- Do not run broad AWF/GitHub validation; use focused tests only.

## Implementation Steps

1. Update the focused recovered-HEAD unit test to expect committed-diff protected-scope validation instead of synthetic dirty-status repair.
2. Import and use the existing `_protected_scope_violations_for_recovered_commit` helper from the fix-pass path.
3. Return the existing protected-scope repair failure result when committed-diff violations are found.
4. Run the focused unit test(s) that cover this path.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py -q -k recovered_head`
  - Passes after implementation.
  - Demonstrates recovered HEAD diff failure, recovered committed-diff violation blocking, and recovered repair failure edges stay covered.

Full AWF/GitHub validation is managed by AWF after agent completion.
