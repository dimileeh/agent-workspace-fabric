# PR614 Shard 6 Repair Start Fixtures Validation

Plan reference: `plans/PR614_SHARD6_REPAIR_START_FIXTURES_PLAN.md`

## Requirement Status

- Complete: Did not switch branches, push, rebase, or run broad
  AWF/GitHub-owned validation.
- Complete: Reproduced representative shard 6 failures locally before editing:
  the CI-fix ownership-repair test returned `REPAIR_START_HEAD_UNAVAILABLE`,
  and the no-mirror candidate test attempted recovery from an unverified
  candidate because the fake command queue only failed the stale anchor lookup.
- Complete: Preserved the original provider recovery, commit-sink precedence,
  and unverified candidate rejection assertions.
- Complete: Added minimum repair-start fixture setup to affected CI-fix tests:
  create the repair worktree, queue clean status, queue `rev-parse HEAD`, and
  queue object-existence success.
- Complete: Updated the no-mirror candidate test to fail both object checks and
  assert both sanitized `cat-file` calls.
- Complete: Ran focused local verification only.
- Complete: Full AWF/GitHub validation, coverage gates, and CI provenance remain
  managed by AWF after agent completion.

## Files Changed

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
- `plans/PR614_SHARD6_REPAIR_START_FIXTURES_PLAN.md`
- `plans/PR614_SHARD6_REPAIR_START_FIXTURES_VALIDATION.md`

## Evidence

- Failing focused repro before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py::test_ci_fix_ownership_repair_failure_blocks_push -q`
  failed with `REPAIR_START_HEAD_UNAVAILABLE` instead of
  `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED`.
- Failing focused repro before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_no_mirror_rejects_unverified_candidate_head -q`
  failed because recovery reached the mocked filesystem recovery path for the
  candidate head.
- Passing affected coverage-edge subset:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py -k 'ci_fix_protected_scope_repair_ownership_repair_failure_returns_failed_push or ci_fix_ownership_repair_failure_blocks_push or ci_fix_records_provider_agent_run_error_before_commit_sink_early_return or ci_fix_preserves_commit_sink_failure_when_provider_recovers' -q`
  passed: `8 passed, 13 deselected in 10.32s`.
- Passing runner-part repro group:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py::test_ci_fix_usage_limit_failure_records_recovery_and_source_cooldown tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py::TestMiscMonitorHelpers::test_commit_dirty_worktree_no_mirror_rejects_unverified_candidate_head -q`
  passed: `2 passed in 3.99s`.
- Line counts:
  `wc -l tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_010.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py`
  reported 1468, 65, and 1419 lines.
- Passing line-limit check:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed: `1 passed in 0.44s`.
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_005.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_003.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_010.py`
  passed: `All checks passed!`.

## Residual Risk

The current remote CI run is for the pre-fix PR head. AWF owns pushing these
local commits and running the full post-agent validation/provenance checks after
this agent phase.
