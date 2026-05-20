# Review 4482045018 Cache, Trust Guard, And TypedDict Plan

## Problem Statement And Scope

PR review comment `issue:4482045018` flags three actionable follow-ups in the
Compose env-file work:

- `_cached_compose_interpolation_keys()` should let waiters short-circuit after
  an unexpected parser exception instead of clearing the inflight marker with no
  cache entry.
- Support-bundle collector kwargs should use the public `ComposeEnvFileInput`
  contract, matching readiness collectors.
- `_trusted_service_compose_env_file()` should verify the resolved local service
  compose path instead of accepting any adjacent file named `local-service.yml`.

Scope is limited to these fixes and focused regression coverage.

## Requirements Checklist

- [ ] Add a regression proving concurrent waiters reuse a cached parser failure
      and do not each retry the same failing Compose parse.
- [ ] Add a regression proving an unrelated absolute `docker/compose` tree is
      not trusted solely because its compose file is named `local-service.yml`.
- [ ] Preserve existing successful cache, service env, and support-bundle
      behavior.
- [ ] Update support-bundle TypedDict annotations to `ComposeEnvFileInput`.
- [ ] Avoid branch changes, push/rebase operations, and unrelated refactors.

## Implementation Steps

1. Add the two focused unit regressions and confirm they fail before source
   changes where practical.
2. Store an exception sentinel in the interpolation-key cache for unexpected
   parser failures, wake inflight waiters, and re-raise the original failure for
   the parsing caller.
3. Raise a fresh exception from the cached failure sentinel for subsequent
   waiters/callers without retaining parser traceback frames or Compose
   contents.
4. Change support-bundle collector TypedDict annotations to
   `ComposeEnvFileInput`.
5. Replace the filename-only trusted-compose guard with
   `_is_local_service_compose_file_path()`.
6. Run targeted tests, lint, and type checks, then write the validation record.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_compose_interpolation_cache_caches_unexpected_parse_failure tests/unit/cli/test_init.py::test_trusted_service_compose_env_file_rejects_unrelated_local_service_file -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_caches_compose_interpolation_keys_until_file_changes tests/unit/service/test_logs.py::test_service_logs_compose_interpolation_cache_serializes_concurrent_misses tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_explicit_null_compose_env_file -q
uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py src/awf/service/support_bundle.py src/awf/cli/main.py tests/unit/service/test_logs.py tests/unit/cli/test_init.py
uv run --python 3.12 --extra dev mypy src/awf
```

The first command should fail before implementation and pass after
implementation. All commands should pass before committing.
