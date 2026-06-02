# Address PRRT_kwDOSJAM6s6GS9S4 Plan

## Problem Statement and Scope

PR review feedback reports that the retry runtime-release gate compares a
`WorkspaceStatus` enum member against `HOST_PORT_TERMINAL_RELEASE_STATUSES`,
which currently contains persisted string values for SQL filters. The fix is
limited to keeping SQL/value comparisons string-based while ensuring the Python
retry gate uses enum members.

## Requirements Checklist

- Add focused regression coverage for the retry runtime-release gate using a
  `WorkspaceStatus` enum-member terminal status tuple.
- Preserve the existing persisted string status tuple for SQL and string-status
  call sites.
- Update `_source_runtime_not_yet_released` to compare enum members against an
  enum-member constant.
- Run targeted validation only; broad AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a focused unit test in `tests/unit/service/test_workspace_retry_port.py`
   that fails if `_source_runtime_not_yet_released` uses the string-valued
   terminal-release tuple.
2. Add an enum-member terminal release constant in
   `src/awf/db/repositories/base.py` and derive the existing string tuple from
   it.
3. Import the enum-member constant in `src/awf/service/workspaces_retry.py` and
   use it for the Python enum comparison.
4. Run the focused new test, then the narrow affected test file if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::<new-test-name> -q`
  passes after implementation and fails before implementation when practical.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
  passes if runtime is available within the focused validation budget.
