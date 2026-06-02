# ADDRESS_THREAD_PRRT_kwDOSJAM6s6GVahZ Plan

## Problem Statement and Scope

PR review thread `PRRT_kwDOSJAM6s6GVahZ` reports that the cleanup worker's
pending planning-scope auto-retry resume scan filters by raw
`WorkerConfig.node_id`. With the default worker config, that compares against
`NULL` instead of the canonical local node id, so the scan misses released
terminal workspaces stamped with `node_id="local"`. Scope is limited to cleanup
candidate node-scope filtering and focused regression coverage.

## Requirements Checklist

- Use the effective worker node id for cleanup node-scope scans.
- Preserve inclusion of legacy rows where `Workspace.node_id` is `NULL`.
- Add a focused regression proving the default worker config releases a
  terminal runtime workspace stamped with `node_id="local"`.
- Add a focused regression proving the default worker config finds a released
  pending planning-scope auto-retry candidate stamped with `node_id="local"`.
- Run only targeted tests for the changed behavior; leave broad AWF/GitHub
  validation to AWF after agent completion.

## Implementation Steps

1. Add a regression test beside terminal runtime release tests for a default
   `WorkerConfig` and `node_id="local"` cleanup candidates.
2. Confirm the planning-scope retry regression fails before the implementation change when
   practical.
3. Update cleanup scans to compare against `effective_worker_config_node_id`.
4. Run the targeted regression test and, if needed, the narrow adjacent cleanup
   test.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_releases_terminal_workspace_on_local_node tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_worker_parts/test_worker_part_042.py`
- Pass criteria: the targeted regression passes with no broad suite, coverage
  gate, frontend build, push, or branch operation executed locally.
