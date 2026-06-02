# ADDRESS_THREAD_PRRT_kwDOSJAM6s6GVahZ Validation

Plan reference: `ADDRESS_THREAD_PRRT_kwDOSJAM6s6GVahZ_PLAN.md`

## Requirement Status

- Complete: Cleanup node-scope scans use `effective_worker_config_node_id`.
- Complete: Legacy `Workspace.node_id IS NULL` rows remain included in both
  cleanup scans.
- Complete: Added a focused regression for default local workers releasing a
  terminal runtime workspace stamped with `node_id="local"`.
- Complete: Added a focused regression for default local workers resuming a
  released pending planning-scope auto-retry workspace stamped with
  `node_id="local"`.
- Complete: Ran focused local checks only. Broad AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/control/worker/cleanup.py`
- `tests/unit/control/test_worker_parts/test_worker_part_042.py`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GVahZ_PLAN.md`
- `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GVahZ_VALIDATION.md`

Fail-first evidence:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node -q`
- Result before implementation: failed because `resumed == []` for a
  `node_id="local"` released retry candidate under default `WorkerConfig`.

Passing checks:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node -q`
- Result after implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_releases_terminal_workspace_on_local_node tests/unit/control/test_worker_parts/test_worker_part_042.py::TestTerminalRuntimeReleasePart003::test_default_local_release_scan_resumes_pending_planning_scope_auto_retry_on_local_node -q`
- Result: 2 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/cleanup.py tests/unit/control/test_worker_parts/test_worker_part_042.py`
- Result: passed.

## Gaps

No planned requirement remains partial or missing. Full repository validation,
coverage gates, and CI-equivalent checks were not run locally per the AWF
workspace contract.
