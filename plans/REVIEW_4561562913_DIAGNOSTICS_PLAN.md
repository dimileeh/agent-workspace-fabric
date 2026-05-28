# Review 4561562913 Diagnostics Plan

## Problem Statement And Scope

Address the current review-level feedback from PR comment `issue:4561562913`.
Scope is limited to:

- adding a diagnostic warning when stored companion normalization preserves raw
  invalid stored data;
- making required Compose secret placeholders describe both missing and empty
  source values because Compose `:?` fails for both cases.

## Requirements Checklist

- Preserve existing invalid stored companion fallback behavior.
- Emit a warning with non-secret companion identity when stored companion schema
  validation fails during normalization.
- Keep raw secret values out of logs, state, and rendered Compose placeholders.
- Update required Compose placeholder text to include both
  `COMPANION_ENV_SECRET_SOURCE_MISSING` and
  `COMPANION_ENV_SECRET_SOURCE_EMPTY`.
- Add or update focused regression coverage for both behaviors.
- Run only targeted tests for the touched behavior; leave broad validation to
  AWF/GitHub after agent completion.

## Implementation Steps

1. Add failing regression coverage for invalid stored companion fallback logging.
2. Update existing required placeholder assertions to expect the combined
   missing-or-empty diagnostic text.
3. Implement the warning log in `workspaces_create`.
4. Update `_environment_secret_compose_ref` for required env secrets.
5. Run focused pytest nodes covering the changed service and companion paths.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_workspace_idempotency.py::test_normalize_stored_companion_warns_and_preserves_invalid_row -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py::test_companion_service_from_materialized_resolves_environment_secret_placeholders tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_resolves_companion_environment_secrets tests/unit/node/test_compose_manager.py::TestRender::test_dind_companion_environment_secret_placeholder_is_rendered_without_raw_value tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::test_companion_env_secret_refresh_preserves_required_compose_interpolation -q`

Pass criteria: each targeted command exits with status 0. Full repository
validation is intentionally not run in the agent phase; AWF/GitHub own broad
validation and provenance after this fix cycle.
