# PRRT_kwDOSJAM6s6Cr0AH Plan

## Problem Statement And Scope

The review thread reports that `workspace_create_payload_matches` can raise a
`TypeError` when a legacy workspace row has `owned_paths = NULL`, because the
matcher passes that value into `list()`. The same thread also asks whether the
idempotency check should be scoped to status or phase.

This fix is scoped to workspace create idempotency replay behavior.

## Requirements Checklist

- Add a regression test proving create-idempotency payload matching treats a
  legacy `NULL` stored `owned_paths` value as an empty list.
- Preserve submitted-list semantics for non-empty `owned_paths`; reordering,
  deduping, and removals remain conflicts.
- Do not add workspace lifecycle status or phase to create payload equality:
  create idempotency is keyed to the original create request and should replay
  the existing workspace in its current lifecycle state.
- Keep changes limited to the idempotency matcher and focused tests.

## Implementation Steps

1. Add a unit regression in `tests/unit/service/test_workspace_idempotency.py`
   that exercises `workspace_create_payload_matches` with `owned_paths=None`.
2. Update `workspace_create_payload_matches` to coerce `None` stored
   `owned_paths` values to `[]` before calling `_owned_path_hints_match`.
3. Add a concise comment documenting why lifecycle status is intentionally not
   part of create-payload equality.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/service/test_workspace_idempotency.py`
  passes.
