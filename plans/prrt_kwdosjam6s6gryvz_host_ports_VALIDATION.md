# PRRT_kwDOSJAM6s6GRYVz Host Ports Validation

Plan reference: `plans/prrt_kwdosjam6s6gryvz_host_ports_PLAN.md`

## Requirement Status

- Complete: Added a failing regression for legacy terminal null-runtime rows with
  declared host ports from both task policy and resolved profile.
- Complete: Preserved modern pre-launch/null-compose rows that did not acquire runtime
  by modeling their modern placement evidence in repository tests and by verifying the
  provisioner regression path.
- Complete: Updated `find_host_port_conflicts` to include terminal rows with runtime
  metadata and legacy null-runtime/no-node/no-reservation rows until release.
- Complete: Kept node-scoped legacy behavior intact by reusing the existing null-node
  fallback for legacy rows that cannot be attributed to one node.
- Complete: Added retry-source coverage and updated the source-runtime guard because
  retry excludes the source workspace from conflict scanning.
- Complete: Ran focused tests and checks only; full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Changed files:

- `src/awf/db/repositories/workspace_repo_host_ports.py`
- `src/awf/service/workspaces_retry.py`
- `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `plans/prrt_kwdosjam6s6gryvz_host_ports_PLAN.md`
- `plans/prrt_kwdosjam6s6gryvz_host_ports_VALIDATION.md`

Focused commands:

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports -q`
  - Failed before implementation as expected: returned no conflicts.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_legacy_null_runtime_source_without_reservation -q`
  - Failed before implementation as expected: did not raise `WorkspaceRetrySourceRuntimeNotReleasedError`.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_failed_pre_runtime_no_compose_project_not_blocking tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_cancelled_pre_runtime_no_compose_project_not_blocking tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_compose_project_name_null_invariant_distinguishes_port_holders -q`
  - Passed: 4 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_004.py::TestRecheckBeforeLaunchFailure::test_recheck_exception_clears_prepublished_compose_project -q`
  - Passed: 1 test.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_legacy_null_runtime_source_without_reservation tests/unit/service/test_workspace_retry_port.py::test_retry_allows_when_source_compose_project_name_is_none -q`
  - Passed: 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
  - Passed: 16 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  - Passed: 36 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/workspace_repo_host_ports.py src/awf/service/workspaces_retry.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py tests/unit/service/test_workspace_retry_port.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/workspace_repo_host_ports.py src/awf/service/workspaces_retry.py`
  - Passed.

## Gaps

No planned implementation gaps remain. Full repository validation, coverage gates, and
CI-equivalent checks were intentionally not run in the agent phase per the AWF workspace
contract.
