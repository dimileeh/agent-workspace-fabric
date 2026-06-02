## Plan Reference

`plans/PRRT_kwDOSJAM6s6GX2bd_PLAN.md`

## Requirement Status

- Complete: Added a regression test for a failed source with host ports,
  stamped `node_id`, null compose metadata, no reservation history, and no
  terminal runtime release event.
- Complete: Preserved retry allowance for early cancelled rows with no runtime
  evidence.
- Complete: Preserved retry allowance for modern pre-launch failed rows with
  reservation evidence.
- Complete: Updated retry source-runtime detection so ambiguous null-compose
  legacy rows remain blocked until a terminal runtime release event exists, even
  when `node_id` is stamped.
- Complete: Used focused local checks only; full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/service/workspaces_retry.py`
- `tests/unit/service/test_workspace_retry_port.py`
- `plans/PRRT_kwDOSJAM6s6GX2bd_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6GX2bd_VALIDATION.md`

Focused checks:

- Before the production fix,
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_node_stamped_legacy_null_runtime_source_without_reservation -q`
  failed with `Failed: DID NOT RAISE WorkspaceRetrySourceRuntimeNotReleasedError`.
- After the production fix,
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_node_stamped_legacy_null_runtime_source_without_reservation -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_allows_early_cancelled_source_without_runtime_evidence tests/unit/service/test_workspace_retry_port.py::test_retry_allows_when_source_compose_project_name_is_none tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_legacy_null_runtime_source_without_reservation tests/unit/service/test_workspace_retry_port.py::test_retry_rejects_node_stamped_legacy_null_runtime_source_without_reservation -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry_port.py`
  passed.

## Gaps

None for this plan. Full repository validation, coverage gates, CI-equivalent
checks, and PR merge gating are intentionally left to AWF/GitHub.
