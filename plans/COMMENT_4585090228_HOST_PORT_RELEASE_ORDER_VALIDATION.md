# Comment 4585090228 Host-Port Release Order Validation

Plan reference: `plans/COMMENT_4585090228_HOST_PORT_RELEASE_ORDER_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Add focused regression coverage proving null `event_order` ties use `WorkspaceEvent.id DESC` | Complete | Added `test_has_terminal_runtime_released_event_null_event_order_tie_uses_event_id` in `tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py`. The regression failed before the production change because the compiled SQL lacked `workspace_events.id DESC`. |
| Update `terminal_runtime_effectively_released_expr` and nearby documentation | Complete | `src/awf/db/repositories/base.py` now orders release/revoke events by `occurred_at DESC`, `event_order DESC NULLS LAST`, and `id DESC`; docstrings describe the full ordering tuple. |
| Preserve existing release/revoke behavior | Complete | The existing tied timestamp/event-order regression passes with the new null-order regression. |
| Track the host-port conflict scan scale concern explicitly | Complete | Added a P2 backlog item in `TODO/pre-gke-industrial-readiness.md` for indexed host-port admission state and supporting workspace-event indexing. |
| Run targeted local validation only | Complete | Focused pytest and ruff checks were run. Full AWF/GitHub validation remains managed by AWF after agent completion. |

## Validation Commands

Failed before implementation, as expected:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::test_has_terminal_runtime_released_event_null_event_order_tie_uses_event_id -q
```

Passed after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::test_has_terminal_runtime_released_event_tied_timestamp_uses_event_order tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py::test_has_terminal_runtime_released_event_null_event_order_tie_uses_event_id -q
uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/base.py tests/unit/db/test_workspace_repository_parts/test_workspace_repository_host_port_collision.py
```

No broad suite, full coverage gate, frontend build, OpenAPI drift check, or
CI-equivalent validation was run in this workspace phase.
