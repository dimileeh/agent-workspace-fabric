# Address Review Comment 4482045018 Plan

## Problem Statement And Scope

PR review comment `issue:4482045018` reports two quality issues in the Compose
env work:

- `src/awf/service/environment.py` holds the Compose interpolation cache lock
  while parsing YAML and collecting keys, so unrelated cache-hit readers can be
  blocked behind a slow cache miss.
- `src/awf/cli/main.py` appends overlay-only assignment lines without the
  newline normalization used for overlaid seed assignments, so a root `.env`
  ending in an overlay-only assignment without a trailing newline can produce a
  seeded `docker/compose/.env` without a terminal newline.

Scope is limited to those two behaviors plus focused regression coverage.

## Requirements Checklist

- Add a regression proving cached Compose interpolation reads can complete while
  another thread is parsing a slow cache miss.
- Preserve the existing same-key concurrent-miss serialization behavior covered
  by `test_service_logs_compose_interpolation_cache_serializes_concurrent_misses`.
- Normalize overlay-only assignment lines before appending them to the merged
  env tail.
- Add a regression for an overlay-only assignment at EOF with no trailing
  newline.
- Keep changes scoped to the implementation, focused unit tests, and required
  plan/validation documents.

## Implementation Steps

1. Add the two focused failing unit tests.
2. Update the Compose interpolation cache to check and store under the lock, but
   parse outside the lock while an in-flight marker serializes same-key misses.
3. Normalize overlay-only assignment lines before passing them through
   `_env_assignment_line_with_key`.
4. Run narrow unit tests for the touched areas.
5. Run formatter/lint/type checks justified by the changed Python files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_compose_interpolation_cache_allows_cached_read_during_slow_miss tests/unit/service/test_logs.py::test_service_logs_compose_interpolation_cache_serializes_concurrent_misses tests/unit/cli/test_init.py::test_merge_env_seed_normalizes_overlay_only_assignment_without_trailing_newline -q`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/environment.py src/awf/cli/main.py tests/unit/service/test_logs.py tests/unit/cli/test_init.py`
  - No lint errors.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - No type errors.
