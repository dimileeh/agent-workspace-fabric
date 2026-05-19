# Review 4482045018 Summary Fixes Plan

## Problem Statement and Scope

Address the remaining actionable feedback from PR review-level comment
`issue:4482045018`.

Local inspection shows the `awf service logs` `compose_env_file` inconsistency is
already fixed and covered by a regression test, so the implementation scope is:

- Preserve root `.env` comments that immediately precede overlay assignments for
  keys already present in the compose env seed template.
- Consolidate the duplicated provider-environment resolution helper shared by
  readiness and support-bundle collection.

## Requirements Checklist

- [ ] Keep `awf service logs` passing only the guarded `compose_env_file` path to
  Docker Compose; do not reintroduce root `.env` as `--env-file`.
- [ ] Add a failing regression for comment context before a root `.env` key that
  overrides a seed-template key.
- [ ] Update `_merge_env_seed_contents()` so the final overlay assignment for a
  seed-template key carries its immediately preceding blank/comment context into
  the merged compose env output.
- [ ] Preserve existing dotenv last-value semantics and root-only overlay context
  behavior.
- [ ] Move duplicated readiness/support-bundle provider-env resolution into one
  shared service helper without changing command behavior.
- [ ] Run focused tests and lint for the touched modules.
- [ ] Write validation evidence and commit the scoped changes locally.

## Implementation Steps

1. Add the env-seeding regression to `tests/unit/cli/test_init.py` and confirm it
   fails against the current merge implementation.
2. Update `_merge_env_seed_contents()` in `src/awf/cli/main.py` to retain leading
   overlay context for seed-matching assignments.
3. Add a shared helper in `src/awf/service/config.py` for provider env resolution,
   then use it from `readiness.py` and `support_bundle.py`.
4. Run focused tests for init seeding, service logs, readiness, and support
   bundle behavior, plus ruff on touched files.
5. Create `plans/REVIEW_4482045018_SUMMARY_FIXES_VALIDATION.md` with
   requirement-by-requirement evidence.
6. Stage only changed files and commit with a conventional review-fix message.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_preserves_context_before_seed_overlay_key -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py::test_service_logs_omits_root_env_file_when_compose_env_is_missing tests/unit/service/test_readiness.py::test_core_readiness_resolves_provider_environment_from_compose_env_file tests/unit/service/test_support_bundle.py::test_support_bundle_resolves_provider_environment_from_compose_env_file -q
uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py src/awf/service/config.py src/awf/service/readiness.py src/awf/service/support_bundle.py tests/unit/cli/test_init.py tests/unit/cli/test_service_cli.py tests/unit/service/test_readiness.py tests/unit/service/test_support_bundle.py
```

All commands must pass after implementation. The first test should fail before
the merge fix is applied.
