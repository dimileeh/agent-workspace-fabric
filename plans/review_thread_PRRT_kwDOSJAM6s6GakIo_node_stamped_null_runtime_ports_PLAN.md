# Review Thread PRRT_kwDOSJAM6s6GakIo Node-Stamped Null Runtime Ports Plan

## Problem Statement And Scope

The PR review reports that `find_host_port_conflicts(..., node_id=<worker>)`
does not include terminal workspaces whose runtime metadata is null, whose
`Workspace.node_id` is stamped to the queried worker, and whose resource
reservation is absent. The cleanup scanner includes those same terminal rows
for the worker because it can derive the default `awf_<workspace_id>` Compose
project, so host-port admission can reuse a companion/profile port before
cleanup records terminal-runtime release.

Scope is limited to host-port conflict detection for terminal null-runtime rows
and a regression test for the reviewed case. Existing explicit pre-launch
failure behavior must remain excluded.

## Requirements Checklist

- Add regression coverage for a terminal null-runtime workspace with:
  - `Workspace.node_id` equal to the queried node.
  - no `ResourceReservation` row.
  - no pre-launch failure event.
  - companion and profile host ports declared.
- Ensure the same row does not block a different node.
- Preserve existing exclusions for explicit pre-launch failures.
- Keep broad AWF/GitHub validation delegated to AWF; run only focused local
  tests for the changed host-port behavior.

## Implementation Steps

1. Add a failing unit test near the existing legacy null-runtime host-port
   tests.
2. Update `find_host_port_conflicts` so terminal null-runtime rows may hold
   ports when they have a reservation, have no node, or are stamped with a node.
3. Re-run the focused regression test and the adjacent host-port collision test
   module if practical.
4. Write validation results to the matching validation document and commit the
   changed files locally.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -k "node_stamped_legacy_terminal_null_runtime_metadata_blocks_declared_host_ports" -q`
  - Passes after the code change; should fail before the code change.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py -q`
  - Passes for the focused host-port collision regression surface.

Full AWF/GitHub validation is intentionally not run in the agent phase per the
workspace contract.
