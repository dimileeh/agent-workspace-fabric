# Review Thread PRRT_kwDOSJAM6s6CsIvR Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CsIvR_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a v2 replay with matching
  `profile_ref` accepts a legacy row whose durable profile reference is only
  `env_profile`.
- Complete: Preserved conflict behavior for a non-matching `profile_ref` in the
  same regression test.
- Complete: Kept implementation local to workspace-create replay matching in
  `src/awf/service/workspaces.py`.
- Complete: Validated with the narrow workspace idempotency unit test file.

## Evidence

Files changed:

- `src/awf/service/workspaces.py`
- `tests/unit/service/test_workspace_idempotency.py`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_create_profile_ref_replays_legacy_env_profile_row_with_missing_requested_tier -q`
  - Before the service fix, this failed with `WorkspaceCreateIdempotencyConflictError`.
  - After the service fix and fixture tightening, it passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
  - Passed: 33 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/service/test_workspace_idempotency.py`
  - Passed.

## Gaps

None.
