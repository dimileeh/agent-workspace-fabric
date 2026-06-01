# Retry Local Node Validation

Plan reference: `plans/RETRY_LOCAL_NODE_PLAN.md`

## Requirement Status

- Complete: When no explicit worker node id is configured, retry placement uses
  the current effective local worker node id (`local`) instead of a stale source
  hostname.
  - Evidence: `src/awf/service/workspaces_retry.py` now uses
    `effective_worker_node_id(resolved_settings)` for retry reservations.
  - Evidence:
    `tests/unit/service/test_workspace_retry_port.py::test_retry_defaults_unset_worker_node_to_local_for_legacy_source_hostname`
    asserts the retry reservation node is `local`.
- Complete: The source runtime-release safety gate still blocks unreleased
  local source runtimes when legacy hostname metadata is normalized to `local`.
  - Evidence:
    `tests/unit/service/test_workspace_retry_port.py::test_retry_blocks_unreleased_legacy_source_hostname_with_local_reservation`
    remains in the focused retry-port test file and passes.
- Complete: Explicit non-local worker node ids continue to override source
  metadata.
  - Evidence: Existing explicit-node retry-port tests passed as part of the
    focused file run.
- Complete: Existing retry reservation creation remains intact so scheduler
  claim and host-port conflict checks see the planned retry node.
  - Evidence: The new regression verifies the persisted retry
    `ResourceReservation` node id.

## Commands Run

- Failed before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_defaults_unset_worker_node_to_local_for_legacy_source_hostname -q`
  - Failure showed retry reservation node was `legacy-container-hostname`
    instead of `local`.
- Passed after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_defaults_unset_worker_node_to_local_for_legacy_source_hostname -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
  - `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces_retry.py tests/unit/service/test_workspace_retry_port.py`

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, and merge gating after completion.

## Gaps

None.
