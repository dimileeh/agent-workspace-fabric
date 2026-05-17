# Review Comment 4306403017 Plan

## Problem Statement And Scope

CodeRabbit review-level comment `4306403017` aggregates outside-diff, inline,
and nitpick findings from PR #260. The valid findings should be fixed locally on
the current AWF-managed branch without pushing, while stale findings should be
verified and documented as no-ops.

## Requirements Checklist

- [x] Rename the public workspace-create nested OpenAPI component schemas so no
  `WorkspaceV2*` component definitions or `$ref`s remain in `openapi.json`.
- [x] Add/adjust OpenAPI tests that fail while `WorkspaceV2*` schemas are still
  exposed.
- [x] Guard workspace idempotency replay helpers against non-mapping
  `resolved_profile` values and reject boolean `requested_tier` values.
- [x] Align validation-provenance merge-candidate test helper task lookup with a
  reused workspace attempt.
- [x] Remove duplicate `/v1/workspaces` expectations from OpenAPI artifact tests.
- [x] Simplify replay tests that now always use `WorkspaceCreateRequest` payloads.
- [x] Bound the Alembic subprocess in task-attempt migration tests with a
  timeout.
- [x] Verify stale/no-op reviewer items without weakening existing tests:
  `test_repository_coverage.py` SQL no-op assertion and the TODO endpoint
  checklist duplicate.
- [x] Regenerate `openapi.json` and verify there is no spec drift.

## Implementation Steps

1. Add or update focused tests first for OpenAPI component names and
   non-mapping `resolved_profile` handling, and run those tests to confirm the
   reported failures where practical.
2. Rename workspace-create nested schema classes in `src/awf/api/schemas.py`
   from legacy `WorkspaceV2*` names to canonical `Workspace*` names.
3. Update `_resolved_profile_requested_tier` to accept only mappings and to
   ignore boolean tiers.
4. Update `_attach_merge_candidate` in
   `tests/unit/api/test_validation_provenance.py` to reuse the existing
   attempt's task when present.
5. Apply the focused test cleanups for OpenAPI duplicate expectations, replay
   tests, and the Alembic subprocess timeout.
6. Regenerate `openapi.json`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_workspace_create_schema_components_use_canonical_v1_names -q`
  should fail before the schema rename and pass after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_stored_profile_and_policy_helpers_handle_missing_or_malformed_data -q`
  should fail before the mapping guard and pass after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py -q`
  should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_uses_post_denial_durable_replay tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_refreshes_preview_after_durable_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_known_replay_key_db_miss_returns_conflict_without_create -q`
  should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_provenance.py -q`
  should pass.
- `uv run --python 3.12 --extra dev pytest tests/unit/db/test_task_attempts.py::TestTaskAttemptMigration::test_task_attempt_migration_creates_tables tests/unit/db/test_repository_coverage.py::test_task_attempt_lock_is_noop_for_non_postgres_dialects -q`
  should pass.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check` should pass after regenerating
  the artifact.
