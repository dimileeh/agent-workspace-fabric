# PR403 Shard And Comment Fix Validation

## Summary

Implemented the PR #403 follow-up fixes required by failed coverage shards and
unresolved review comments:

- Restored Compose-compatible interpolation for root/local `.env` reads while
  preserving single-quoted `${...}` credentials as literals.
- Taught shared CLI API-token headers to fall back to the root Compose local
  default token when neither `--api-token` nor shell `AWF_API_TOKEN` is set.
- Fixed shard 6 bootstrap fixture drift by writing root `compose.yaml` in the
  fake checkout used by mount-propagation bootstrap tests.
- Fixed shard 6 PR-monitor grace tests by using an elapsed monotonic timestamp
  instead of `0.0`, which only worked on long-running machines.
- Fixed shard 8 line-limit failure by splitting bootstrap asset-resolution tests
  into `test_bootstrap_part_005.py` instead of weakening the guard.

## Validation

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py -q`
  - Passed: 15 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_common_helpers.py -q`
  - Passed: 11 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py tests/unit/service/test_config_parts/test_config_part_003.py -q`
  - Passed: 115 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap_parts/test_bootstrap_part_001.py tests/unit/service/test_bootstrap_parts/test_bootstrap_part_004.py tests/unit/service/test_bootstrap_parts/test_bootstrap_part_005.py -q`
  - Passed: 50 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_recovery_grace.py -q`
  - Passed: 18 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passed: 1 test.
- `uv run --python 3.12 --extra dev ruff check ...`
  - Passed for touched Python files.
- `uv run --python 3.12 --extra dev ruff format --check ...`
  - Passed for touched Python files.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.
- `uv run --python 3.12 --extra dev pytest --splits 8 --group 6 --timeout=300 -q`
  - Passed: 1367 tests, 9568 deselected.
- `uv run --python 3.12 --extra dev pytest --splits 8 --group 8 --timeout=300 -q`
  - Passed: 1366 tests, 9569 deselected.
- `git diff --check`
  - Passed.

## Remaining Risk

- The new CI run still needs to complete on GitHub after push, but the two
  failed shard shapes from the previous run now pass locally.
