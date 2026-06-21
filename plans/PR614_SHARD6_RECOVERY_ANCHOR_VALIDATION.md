# PR614 Shard 6 Recovery Anchor Validation

## Result

Implemented the shard 6 recovery-anchor fix from
`plans/PR614_SHARD6_RECOVERY_ANCHOR_PLAN.md`.

The root causes were:

- `_repair_operation_start_head_result` validated no-mirror fallback heads by
  shelling out to `cat-file`, bypassing the injectable HEAD-object guard used by
  the failing no-mirror tests.
- Missing-HEAD recovery could keep an unverified stale operation-start anchor
  when no mirror was available, instead of preferring the open merge candidate.
- The PR monitor unit autouse fixture masked the real mirror commit guard for
  missing-HEAD recovery tests that had queued mirror-guard command results,
  shifting fake command output and hiding the intended behavior.

## Focused Checks

Initial representative repro failed before the fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_repair_operation_start_head_accepts_mocked_no_mirror_fallback tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_falls_back_from_stale_start_head -q
```

Passing focused checks after the fix:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_repair_operation_start_head_accepts_mocked_no_mirror_fallback tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_repair_operation_start_head_rejects_no_mirror_fallback_when_guard_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_runs_precommit_gates tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_fails_closed_when_recovered_diff_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_blocks_on_ownership_failure tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_blocks_recovered_protected_scope tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_commit_dirty_worktree_missing_head_recovery_commits_protected_repair_residue tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_missing_head_falls_back_from_stale_start_head -q
```

Result: `8 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py -k 'commit_dirty_worktree_missing_head_recovery' -q
```

Result: `6 passed, 17 deselected`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/conftest.py
```

Result: passed.

## Deferred Validation

Full AWF/GitHub-owned broad validation and coverage gates were not run locally,
per the workspace contract. AWF will run those after agent completion.
