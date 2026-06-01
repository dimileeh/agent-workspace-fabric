# Retry Local Node Plan

## Problem Statement And Scope

An unresolved PR review thread reports that retrying an older failed workspace
can create the retry reservation on a stale hostname when `AWF_WORKER_NODE_ID`
is unset. Current local workers/provisioners default their effective node id to
`local`, and scheduler admission scopes requested work by the active reservation
node. A retry reservation stamped with the old hostname can therefore stay
requested and unclaimable by the only local worker.

Scope is limited to retry-time node selection in
`src/awf/service/workspaces_retry.py` and focused regression coverage in the
existing retry-port unit tests.

## Requirements Checklist

- When no explicit worker node id is configured, retry placement must use the
  current effective local worker node id (`local`) instead of a stale source
  hostname.
- The source runtime-release safety gate must still block unreleased local
  source runtimes when legacy hostname metadata is normalized to `local`.
- Explicit non-local worker node ids must continue to override source metadata.
- Existing retry reservation creation must remain intact so scheduler claim and
  host-port conflict checks see the planned retry node.

## Implementation Steps

1. Add a regression test for an upgraded local install: source workspace has a
   stale `source.node_id`, its runtime is released, `worker_node_id` is unset,
   and retry reservation should be created on `local`.
2. Update retry node resolution to compute the current effective worker node id
   when no explicit configured node exists, and normalize legacy source
   hostname metadata consistently for the source runtime comparison.
3. Run the new focused test first to confirm the current behavior fails.
4. Run the focused retry-port test file after implementation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py::test_retry_defaults_unset_worker_node_to_local_for_legacy_source_hostname -q`
  - Passes only when the retried reservation node is `local`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_retry_port.py -q`
  - Passes with existing retry-port safety regressions unchanged.

Full AWF/GitHub validation remains managed by AWF after agent completion.
