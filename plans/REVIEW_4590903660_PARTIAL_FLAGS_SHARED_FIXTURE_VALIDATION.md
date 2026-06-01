# Review 4590903660 Partial Flags and Shared Fixture Validation

Plan reference:
`REVIEW_4590903660_PARTIAL_FLAGS_SHARED_FIXTURE_PLAN.md`

## Requirement Status

- Add a focused regression for partial repository merge flag payloads: Complete.
- Require all three repository merge flags to be present before deriving enabled
  methods: Complete.
- Preserve explicit-all-false behavior as a valid empty repository merge policy:
  Complete.
- Move `DefaultMergeMethodGitHubClient` to a shared test helper importable by
  both unit and integration tests: Complete.
- Keep existing unit fixture imports compatible for unit tests that already
  consume `_monitor_runner_fixtures`: Complete.
- Run only focused validation; AWF/GitHub owns broad validation after agent
  completion: Complete.

## Evidence

Files changed:

- `src/awf/common/github_client.py`
- `tests/shared/__init__.py`
- `tests/shared/monitor_runner.py`
- `tests/unit/runtime/_monitor_runner_fixtures.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py`
- `tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
- `tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
- `plans/REVIEW_4590903660_PARTIAL_FLAGS_SHARED_FIXTURE_PLAN.md`
- `plans/REVIEW_4590903660_PARTIAL_FLAGS_SHARED_FIXTURE_VALIDATION.md`

Focused checks:

- Pre-fix TDD check:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k fetch_repo_merge_methods`
  failed as expected with
  `test_fetch_repo_merge_methods_rejects_partial_repo_flags` not raising
  `GitHubClientError`.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k fetch_repo_merge_methods`
  passed with `4 passed, 46 deselected`.
- `uv run --python 3.12 --extra dev pytest --collect-only tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py -q`
  passed with `74 tests collected`.
- `rg -n "from tests\\.unit\\.runtime\\._monitor_runner_fixtures import DefaultMergeMethodGitHubClient" tests/integration || true`
  returned no matches.
- `uv run --python 3.12 --extra dev python - <<'PY' ...`
  confirmed `_monitor_runner_fixtures.DefaultMergeMethodGitHubClient` re-exports
  the shared helper.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/shared/monitor_runner.py tests/unit/runtime/_monitor_runner_fixtures.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/common/github_client.py tests/shared/monitor_runner.py tests/unit/runtime/_monitor_runner_fixtures.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  passed.

Full AWF/GitHub validation, whole-repository tests, full coverage gates, and
CI-equivalent checks were not run in the agent phase per the AWF workspace
contract.
