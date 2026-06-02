# Address PRRT_kwDOSJAM6s6GS9S4 Validation

Plan reference: `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GS9S4_PLAN.md`

## Requirement Status

- Complete: Added focused regression coverage in
  `tests/unit/service/test_workspace_retry_port.py` for the retry
  runtime-release gate using `WorkspaceStatus` enum members.
- Complete: Preserved `HOST_PORT_TERMINAL_RELEASE_STATUSES` as persisted string
  values for SQL/string-status call sites.
- Complete: Added `HOST_PORT_TERMINAL_RELEASE_WORKSPACE_STATUSES` and updated
  `_source_runtime_not_yet_released` to compare enum members against it.
- Complete: Ran focused local validation only. Full AWF/GitHub validation,
  provenance, and merge gating remain managed by AWF after agent completion.

## Evidence

- Changed `src/awf/db/repositories/base.py` to define the enum-member terminal
  release tuple and derive the existing persisted-value tuple from it.
- Changed `src/awf/service/workspaces_retry.py` to use the enum-member tuple
  for the Python-side retry runtime-release comparison.
- Changed `tests/unit/service/test_workspace_retry_port.py` to add
  `test_source_runtime_release_gate_uses_enum_terminal_statuses`.
- Confirmed the focused regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_source_runtime_release_gate_uses_enum_terminal_statuses -q`
  failed with missing `HOST_PORT_TERMINAL_RELEASE_WORKSPACE_STATUSES`.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_source_runtime_release_gate_uses_enum_terminal_statuses -q`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry_port.py`
  passed.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
  passed with 19 tests.
- After implementation:
  `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/base.py src/awf/service/workspaces_retry.py`
  passed.

## Gaps

None. Broad repository validation was intentionally not run because AWF/GitHub
owns broad validation for this workspace.
