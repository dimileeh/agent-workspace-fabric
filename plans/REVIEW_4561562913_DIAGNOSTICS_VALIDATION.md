# Review 4561562913 Diagnostics Validation

Plan reference: `plans/REVIEW_4561562913_DIAGNOSTICS_PLAN.md`

## Requirement Status

- Complete: Preserved invalid stored companion fallback behavior by still
  returning the raw stored mapping on schema validation failure.
- Complete: Added a warning event with `companion_name` when stored companion
  normalization validation fails.
- Complete: Kept raw secret values out of the updated log path and rendered
  Compose placeholders.
- Complete: Updated required Compose placeholders to include both
  `COMPANION_ENV_SECRET_SOURCE_MISSING` and
  `COMPANION_ENV_SECRET_SOURCE_EMPTY`.
- Complete: Added or updated focused regression coverage for the fallback log
  and required placeholder text.
- Complete: Ran only targeted local checks; full AWF/GitHub validation remains
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/service/workspaces_create.py`
- `src/awf/node/companion_services.py`
- `tests/unit/service/test_workspace_idempotency.py`
- `tests/unit/node/test_companion_services.py`
- `tests/unit/node/test_stack_launcher.py`
- `tests/unit/node/test_compose_manager.py`
- `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_normalize_stored_companion_warns_and_preserves_invalid_row -q`
  - Pre-implementation result: failed because no warning event was emitted.
  - Post-implementation result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_service_from_materialized_resolves_environment_secret_placeholders tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_resolves_companion_environment_secrets -q`
  - Pre-implementation result: failed because required placeholders only
    included `COMPANION_ENV_SECRET_SOURCE_MISSING`.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_service_from_materialized_resolves_environment_secret_placeholders tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_resolves_companion_environment_secrets tests/unit/node/test_compose_manager.py::TestRender::test_dind_companion_environment_secret_placeholder_is_rendered_without_raw_value tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::test_companion_env_secret_refresh_preserves_required_compose_interpolation -q`
  - Post-implementation result: passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_services.py src/awf/service/workspaces_create.py tests/unit/service/test_workspace_idempotency.py tests/unit/node/test_companion_services.py tests/unit/node/test_stack_launcher.py tests/unit/node/test_compose_manager.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`
  - Result: passed.

No broad AWF/GitHub-owned validation was run in the agent phase.

## Remaining Gaps

None for this review-comment scope.
