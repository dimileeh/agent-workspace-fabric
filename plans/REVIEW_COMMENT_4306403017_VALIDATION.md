# Review Comment 4306403017 Validation

Plan reference: `plans/REVIEW_COMMENT_4306403017_PLAN.md`

## Requirement Status

- Complete: Renamed workspace-create nested schema classes to canonical v1
  names in `src/awf/api/schemas.py` and regenerated `openapi.json`.
- Complete: Added
  `test_workspace_create_schema_components_use_canonical_v1_names` to prove no
  `WorkspaceV2*` components or refs leak through OpenAPI.
- Complete: `_resolved_profile_requested_tier` now requires mapping-shaped
  profile and validation values, and rejects boolean tiers.
- Complete: `_attach_merge_candidate` now looks up the task from a reused
  attempt before creating/updating the merge candidate.
- Complete: Removed duplicated `/v1/workspaces` expectations in
  `tests/unit/api/test_openapi_artifact.py`.
- Complete: Simplified the three replay tests in
  `tests/unit/api/test_workspaces.py` to call the canonical
  `WorkspaceCreateRequest` path without legacy shape branches.
- Complete: Added a 300-second timeout and explicit timeout failure message to
  the Alembic subprocess in `tests/unit/db/test_task_attempts.py`.
- Complete: Verified no-op reviewer items without changing them:
  `tests/unit/db/test_repository_coverage.py` already asserts
  `session.executed == []`, and `TODO/pre-gke-industrial-readiness.md` lines
  626-628 list `POST /v1/workspaces` only once.

## Evidence

Changed files:

- `src/awf/api/schemas.py`
- `src/awf/service/workspaces.py`
- `tests/unit/api/test_openapi_artifact.py`
- `tests/unit/api/test_validation_provenance.py`
- `tests/unit/api/test_workspaces.py`
- `tests/unit/db/test_task_attempts.py`
- `openapi.json`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py::test_workspace_create_schema_components_use_canonical_v1_names -q`
  failed before the schema rename and passed after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_stored_profile_and_policy_helpers_handle_missing_or_malformed_data -q`
  failed before the mapping guard and passed after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_validation_provenance.py::test_attach_merge_candidate_reuses_existing_attempt_task -q`
  failed before the helper fix and passed after it.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py src/awf/service/workspaces.py tests/unit/api/test_openapi_artifact.py tests/unit/api/test_validation_provenance.py tests/unit/api/test_workspaces.py tests/unit/db/test_task_attempts.py`
  passed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_openapi_artifact.py tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_uses_post_denial_durable_replay tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_rate_limited_workspace_create_refreshes_preview_after_durable_miss tests/unit/api/test_workspaces.py::TestCreateWorkspace::test_known_replay_key_db_miss_returns_conflict_without_create tests/unit/api/test_workspaces.py::TestCreateWorkspacePolicyMetadata::test_stored_profile_and_policy_helpers_handle_missing_or_malformed_data tests/unit/api/test_validation_provenance.py tests/unit/db/test_task_attempts.py::TestTaskAttemptMigration::test_task_attempt_migration_creates_tables tests/unit/db/test_repository_coverage.py::test_task_attempt_lock_is_noop_for_non_postgres_dialects -q`
  passed: 60 tests.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
- `git diff --check`
  passed.

## Gaps

None.
