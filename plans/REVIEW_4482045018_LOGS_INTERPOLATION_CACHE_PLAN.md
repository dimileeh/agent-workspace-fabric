# Review 4482045018 Logs Interpolation Cache Plan

## Problem Statement And Scope

Address the remaining actionable parts of review-level comment `issue:4482045018`
without weakening existing regressions. The scope is limited to local service
logs Compose interpolation-key parsing and explanatory documentation around the
minimal Docker subprocess environment.

The state-directory concern is already satisfied by the current CLI output:
`awf init` prints the resolved state directory immediately before the
`created:` line, including the value selected from the service env.

## Requirements Checklist

- Add a regression proving repeated `awf service logs` calls against an
  unchanged Compose file do not repeatedly parse the YAML.
- Keep the existing regression proving edits to the same Compose file path are
  observed by later `awf service logs` calls.
- Cache Compose interpolation key discovery in a way that is invalidated when
  the Compose file changes.
- Add an explanatory comment documenting why equal env-file values can be
  omitted from the explicit subprocess env.
- Run focused logs tests and static checks for the touched files.
- Create a validation document against this plan.
- Commit only this review fix cycle's changed files.

## Implementation Steps

1. Add a failing focused test in `tests/unit/service/test_logs.py` for repeated
   unchanged Compose YAML parsing.
2. Implement metadata-keyed caching in `src/awf/service/logs.py`.
3. Add the invariant comment in `_compose_interpolation_environ`.
4. Run the new test, the existing same-path reload test, then the logs test
   module and static checks.
5. Record validation evidence in
   `plans/REVIEW_4482045018_LOGS_INTERPOLATION_CACHE_VALIDATION.md`.
6. Stage and commit the scoped files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_caches_compose_interpolation_keys_until_file_changes tests/unit/service/test_logs.py::test_service_logs_reloads_compose_interpolation_keys_when_file_changes -q`
  - Passes, proving both cache reuse and invalidation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
  - Passes with the full logs helper unit surface.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`
  - Passes with no lint violations.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes with no type errors.
