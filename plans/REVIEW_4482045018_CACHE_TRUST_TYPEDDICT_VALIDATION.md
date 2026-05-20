# Review 4482045018 Cache, Trust Guard, And TypedDict Validation

## Plan Conformance

- Added a regression for concurrent callers after an unexpected Compose YAML
  parser exception. Before the fix it failed because each waiter retried the
  same failing parse (`parse_count == 4` instead of `1`).
- Added a regression for `_trusted_service_compose_env_file()` rejecting an
  unrelated absolute `docker/compose/local-service.yml` tree. Before the fix it
  failed because the unrelated `.env` was accepted.
- Updated `_cached_compose_interpolation_keys()` to cache a lightweight failure
  sentinel for unexpected parser exceptions, wake waiters, and avoid retaining
  parser traceback frames.
- Updated support-bundle collector kwargs to use `ComposeEnvFileInput`.
- Replaced the filename-only trusted-compose guard with the existing resolved
  `_is_local_service_compose_file_path()` helper.

## Verification Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_compose_interpolation_cache_caches_unexpected_parse_failure tests/unit/cli/test_init.py::test_trusted_service_compose_env_file_rejects_unrelated_local_service_file -q
```

Result before implementation: failed as expected with 2 failures.

Result after implementation: passed, `2 passed in 0.94s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_caches_compose_interpolation_keys_until_file_changes tests/unit/service/test_logs.py::test_service_logs_compose_interpolation_cache_serializes_concurrent_misses tests/unit/service/test_support_bundle.py::test_support_bundle_forwards_explicit_null_compose_env_file -q
```

Result: passed, `3 passed in 0.75s`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py src/awf/service/support_bundle.py src/awf/cli/main.py tests/unit/service/test_logs.py tests/unit/cli/test_init.py
```

Result: passed, `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check src/awf/service/environment.py src/awf/service/support_bundle.py src/awf/cli/main.py tests/unit/service/test_logs.py tests/unit/cli/test_init.py
```

Result after formatting `src/awf/service/environment.py`: passed,
`5 files already formatted`.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: passed, `Success: no issues found in 158 source files`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/cli/test_init.py tests/unit/service/test_support_bundle.py -q
```

Result: passed, `185 passed in 8.55s`.

An optional full `tests/unit` run was started but stopped after slow progress
through unrelated tests; the targeted and touched-module validation above
completed successfully.

## Gaps

No implementation gaps found for the review comment.
