# Host Port Reserved Legacy Launch Validation

Plan reference: `HOST_PORT_RESERVED_LEGACY_LAUNCH_PLAN.md`

## Requirement Status

- Regression for a terminal null-compose workspace with a reservation:
  Complete. Added
  `test_reserved_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports`.
- Preserve explicit pre-launch escape hatch:
  Complete. Added
  `test_pre_launch_reserved_null_runtime_metadata_does_not_block_host_ports`.
- Keep node-scoped reservation behavior intact:
  Complete. The reserved legacy regression verifies conflicts on `node-a` and
  no conflict on `node-b`; the affected host-port collision module remains
  green.
- Avoid broad AWF/GitHub-owned validation:
  Complete. Only focused repository tests and touched-file lint were run.

## Evidence

- Confirmed the new reserved legacy regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_reserved_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports -q`
  failed with no conflicts returned.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_reserved_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::TestCrossNodeAndEdgeCases::test_pre_launch_reserved_null_runtime_metadata_does_not_block_host_ports -q`
  passed, 2 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  passed, 38 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/workspace_repo_host_ports.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`
  passed.

Full AWF/GitHub validation is managed after agent completion.

## Gaps

None.
