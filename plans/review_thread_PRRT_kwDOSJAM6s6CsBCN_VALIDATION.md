# PRRT_kwDOSJAM6s6CsBCN Validation

Plan reference: `review_thread_PRRT_kwDOSJAM6s6CsBCN_PLAN.md`

## Requirement Status

- Complete: Preserve replay when an existing legacy row has
  `requires_database=True`, `profile_ref=None`, and `env_profile=None`, and the
  replay request resolves to the database compatibility profile.
  Evidence: `tests/unit/service/test_workspace_idempotency.py` adds
  `test_create_database_profile_replays_legacy_requires_database_row`.
- Complete: Keep non-database named profile requests from matching legacy rows
  with no profile identity.
  Evidence: The regression test asserts a `profile_ref="python"` payload does
  not match the legacy database row.
- Complete: Keep current rich create and auto-profile idempotency behavior
  unchanged.
  Evidence: Full `tests/unit/service/test_workspace_idempotency.py` passed.
- Complete: Validate with the narrowest relevant unit test command.
  Evidence: Commands below passed.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_create_database_profile_replays_legacy_requires_database_row -q`
- `uv run --python 3.12 --extra dev ruff format src/awf/service/workspaces.py tests/unit/service/test_workspace_idempotency.py`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/service/workspaces.py tests/unit/service/test_workspace_idempotency.py`
- `uv run --python 3.12 --extra dev mypy src/awf/service/workspaces.py`

## Gaps

None.
