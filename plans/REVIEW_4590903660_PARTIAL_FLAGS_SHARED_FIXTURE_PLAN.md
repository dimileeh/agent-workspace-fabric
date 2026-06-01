# Review 4590903660 Partial Flags and Shared Fixture Plan

## Problem Statement and Scope

Address the remaining actionable findings from review-level comment `issue:4590903660`
on PR #353:

- `GitHubClient.fetch_repo_merge_methods` currently rejects only responses where
  all merge-method flags are absent; partial responses should also be treated as
  anomalous API payloads.
- Integration tests import `DefaultMergeMethodGitHubClient` from a unit-test
  fixture module, creating cross-layer coupling.

Scope is limited to the GitHub client merge-method guard, its focused unit tests,
and relocating the shared merge-method default client test double.

## Requirements Checklist

- Add a focused regression for partial repository merge flag payloads.
- Require all three repository merge flags to be present before deriving enabled
  methods.
- Preserve explicit-all-false behavior as a valid empty repository merge policy.
- Move `DefaultMergeMethodGitHubClient` to a shared test helper importable by
  both unit and integration tests.
- Keep existing unit fixture imports compatible for unit tests that already
  consume `_monitor_runner_fixtures`.
- Run only focused validation; AWF/GitHub owns broad validation after agent
  completion.

## Implementation Steps

1. Add the partial-flag regression to the existing GitHub client merge-method
   tests and confirm it fails against the current guard.
2. Update `fetch_repo_merge_methods` to collect and reject any missing merge
   flags.
3. Add a shared test helper module for `DefaultMergeMethodGitHubClient`.
4. Replace the unit-fixture-local class with an import from the shared helper and
   update integration test imports to use the shared helper directly.
5. Run focused pytest and ruff checks for the touched files.
6. Record validation evidence in
   `plans/REVIEW_4590903660_PARTIAL_FLAGS_SHARED_FIXTURE_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client_parts/test_github_client_part_004.py -q -k fetch_repo_merge_methods`
  passes.
- `uv run --python 3.12 --extra dev pytest --collect-only tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py -q`
  collects the touched integration modules without import errors.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/github_client.py tests/shared/monitor_runner.py tests/unit/runtime/_monitor_runner_fixtures.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_002.py tests/integration/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py tests/unit/common/test_github_client_parts/test_github_client_part_004.py`
  passes.

Full repository validation, coverage gates, and CI-equivalent checks are not run
in the agent phase per the AWF workspace contract.
