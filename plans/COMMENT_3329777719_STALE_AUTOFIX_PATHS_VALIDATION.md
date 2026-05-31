# Comment 3329777719 Stale Autofix Paths Validation

Plan reference: `COMMENT_3329777719_STALE_AUTOFIX_PATHS_PLAN.md`

## Requirement Status

- Add a regression test proving protected-scope repair refreshes the dirty-path
  safety boundary used by deterministic pre-commit autofix retry: Complete.
- Keep the retry helper's subset safety intact for unrelated dirty paths:
  Complete. The helper was not relaxed; the caller now supplies the refreshed
  operation dirty paths after protected-scope repair.
- Update implementation to pass the post-repair dirty paths to the retry helper:
  Complete.
- Run only focused tests/checks for the changed runtime behavior: Complete.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `plans/COMMENT_3329777719_STALE_AUTOFIX_PATHS_PLAN.md`
- `plans/COMMENT_3329777719_STALE_AUTOFIX_PATHS_VALIDATION.md`

Focused verification:

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k refreshed`
  failed because the autofix retry saw
  `operation_dirty_paths=['.github/workflows/ci.yml']` while the post-repair
  dirty path was `src/awf/example.py`.
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k refreshed`
  passed with `1 passed, 23 deselected`.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
  passed.
- Related safety check:
  `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_commit_dirty_worktree_uses_refreshed_paths_for_protected_repair_autofix_retry tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_003.py::test_commit_dirty_worktree_does_not_retry_unowned_autofix_dirty_paths`
  passed with `2 passed`.

Full AWF/GitHub validation was not run in the agent phase per the workspace
contract; AWF owns broad validation after agent completion.
