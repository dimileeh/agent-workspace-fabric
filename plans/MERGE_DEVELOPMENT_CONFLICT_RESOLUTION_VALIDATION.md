# Merge Development Conflict Resolution Validation

Plan reference: `plans/MERGE_DEVELOPMENT_CONFLICT_RESOLUTION_PLAN.md`

## Requirement status

- Preserve the intent of both the PR branch and `origin/development`: Complete.
  The resolution keeps the PR provider-environment safeguards and bootstrap
  asset handling while retaining development's source-root Compose env discovery
  and Compose interpolation tests.
- Prefer base-branch semantics where a hunk cannot be reconciled confidently:
  Complete. Shared env-file lookup now uses the development resolver, with the
  PR's provider trust checks layered on top for provider auth loading.
- Remove all conflict markers from the conflicted files: Complete.
- Keep unrelated existing worktree changes intact: Complete. Edits were limited
  to the four conflicted files plus required plan/validation documents.
- Run the narrowest useful validation for the touched service config/bootstrap
  behavior: Complete.
- Stage the resolved files and commit locally with a conventional merge message:
  Complete. This validation document is included in the merge-resolution commit.

## Evidence

- Conflict scan:
  `rg -n "<<<<<<<|=======|>>>>>>>" src/awf/service/bootstrap.py src/awf/service/config.py tests/integration/test_local_service_compose.py tests/unit/service/test_config.py`
  returned no matches.
- Formatting safety:
  `git diff --check` passed.
- Targeted tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py tests/unit/service/test_bootstrap.py tests/integration/test_local_service_compose.py -q`
  passed with `158 passed`.
