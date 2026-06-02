# Comment 4585090228 Host-Port Release Order Plan

## Problem Statement and Scope

Review-level comment `issue:4585090228` raises two host-port admission follow-ups:

- `terminal_runtime_effectively_released_expr` orders release/revoke events by
  `occurred_at` and `event_order`, but lacks a final `WorkspaceEvent.id`
  tiebreaker when both values tie and `event_order` is `NULL`.
- `find_host_port_conflicts` intentionally scans active and terminal-unreleased
  workspace rows and parses ports in Python; this needs durable scale tracking
  before AWF reaches high workspace counts.

Scope is limited to deterministic release/revoke event ordering, focused
regression coverage, and backlog tracking for the known host-port admission
scale constraint. No schema, index, or query-shape optimization is attempted in
this review fix.

## Requirements Checklist

- Add focused regression coverage proving null `event_order` ties use
  `WorkspaceEvent.id DESC` as the final ordering key.
- Update `terminal_runtime_effectively_released_expr` and its nearby
  documentation to include the final `id DESC` tiebreaker.
- Preserve existing release/revoke behavior for normal timestamp and
  event-order cases.
- Track the host-port conflict scan scale concern as an explicit future item
  without changing current admission semantics.
- Run targeted local validation only; full AWF/GitHub validation remains owned
  by AWF after agent completion.

## Implementation Steps

1. Add a focused failing regression beside existing host-port collision
   release/revoke tests.
2. Run that single regression before the production change where practical.
3. Add `WorkspaceEvent.id.desc()` to the release/revoke ordering helper and
   update the docstrings that describe the ordering tuple.
4. Add a concise P2 backlog item for indexed host-port admission state.
5. Re-run the focused regression and nearby tied-ordering test, then run
   focused ruff on touched files.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::test_has_terminal_runtime_released_event_null_event_order_tie_uses_event_id -q
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::test_has_terminal_runtime_released_event_tied_timestamp_uses_event_order tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::test_has_terminal_runtime_released_event_null_event_order_tie_uses_event_id -q
uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py
```

All focused commands must pass. Do not run full coverage, whole-repository unit
suites, frontend builds, OpenAPI drift checks, or CI-equivalent validation in
this workspace phase.
