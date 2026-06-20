# PRRT_kwDOSJAM6s6KxJt1 Branch Ref Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6KxJt1_BRANCH_REF_PLAN.md`

## Requirement Status

- Fail closed before `git update-ref` when the resolved worktree ref differs
  from the workspace's expected local branch ref: Complete.
- Preserve existing missing-HEAD recovery behavior when the resolved ref matches
  the workspace branch: Complete.
- Keep the fix scoped to the PR monitor recovery path: Complete.
- Run only targeted checks and leave broad AWF/GitHub validation to AWF after
  agent completion: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `plans/PRRT_kwDOSJAM6s6KxJt1_BRANCH_REF_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6KxJt1_BRANCH_REF_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch -q` passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_updates_expected_branch_ref -q` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_repair.py` passed.

Also attempted:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -q` failed in unrelated existing test `test_pre_push_validation_fix_pass_recovers_missing_head` because the fixture does not mock `remote_repair.repair_mirror_hooks_path` for the nested commit path. This was not changed for this review-thread fix.

Full AWF/GitHub validation is managed by AWF after agent completion.
