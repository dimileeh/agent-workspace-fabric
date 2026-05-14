# Review Comment 4445667428 Transition Atomicity Plan

## Problem Statement and Scope

PR review comment `issue:4445667428` flags that `WorkspaceRepository.transition_if_current`
lost its unconditional atomic status guard on non-PostgreSQL dialects after the
implementation moved from one `UPDATE ... WHERE status = ... RETURNING ...`
statement to `SELECT` plus ORM mutation. PostgreSQL remains protected by
`SELECT FOR UPDATE`, but dialects without row locks can let two concurrent
callers pass the status check before either writes.

Scope is limited to restoring the atomic status guard for the non-PostgreSQL
`transition_if_current` path while preserving the PostgreSQL row-lock path and
existing transition side effects.

## Requirements

- Add regression coverage proving the non-PostgreSQL path uses a single
  status-guarded `UPDATE` to claim the transition before side effects run.
- Preserve PostgreSQL behavior that locks the workspace row before transition
  side effects.
- Preserve transition side effects: event order/version advancement, task
  attempt status sync, monitor-start timestamp handling, merge-candidate
  lifecycle updates, resource release, and `workspace.state_changed` event
  creation.
- Keep changes scoped to `WorkspaceRepository.transition_if_current` and focused
  repository tests.

## Implementation Steps

1. Add a failing focused repository test for the non-PostgreSQL transition claim
   statement shape.
2. Add a non-PostgreSQL `transition_if_current` path that performs a single
   `UPDATE workspaces ... WHERE id/status/extra_conditions ... RETURNING` before
   reloading the row and running existing side effects.
3. Factor the shared post-claim transition side effects only as much as needed
   to keep PostgreSQL and fallback behavior aligned.
4. Run focused tests and lint/type checks for the touched files.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents::test_transition_if_current_non_postgres_claim_uses_status_guarded_update -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_workspace_repository.py::TestAddEvents::test_transition_if_current_reserves_event_order_through_shared_helper tests/unit/db/test_repository_coverage.py::test_workspace_transition_if_current_releases_resources_and_claims_are_owned -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories.py tests/unit/db/test_workspace_repository.py`
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories.py`
