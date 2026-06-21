# PRRT_kwDOSJAM6s6K0Bba Plan

## Problem Statement and Scope

An inline review reports that recovered HEAD protected-scope repair failures return
`PROTECTED_SCOPE_REPAIR_FAILED`, but push result classification does not treat that
reason as a terminal protected-scope block. Scope is limited to reason-code
classification and focused regression coverage for the reported retry risk.

## Requirements Checklist

- Verify the reported code path returns `PROTECTED_SCOPE_REPAIR_FAILED`.
- Ensure `_GitPushResult` treats `PROTECTED_SCOPE_REPAIR_FAILED` as terminal.
- Preserve protected-scope outcome classification for this reason.
- Add focused regression coverage without broad repository validation.
- Do not push or switch branches.

## Implementation Steps

1. Add a regression test in the existing push outcome classification test module.
2. Confirm the new regression fails before implementation when practical.
3. Import `_PROTECTED_SCOPE_REPAIR_FAILED_REASON` into `remote_ops.py`.
4. Include `_PROTECTED_SCOPE_REPAIR_FAILED_REASON` in the protected-scope blocked
   predicate so terminal and outcome mapping follow the existing protected-scope path.
5. Run the focused regression test file or selected tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_remote_ops.py -q`
  passes.
- Full AWF/GitHub validation is intentionally not run inside the agent phase.
