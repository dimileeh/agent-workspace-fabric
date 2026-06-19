# PRRT_kwDOSJAM6s6K3hi2 Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K3hi2_PLAN.md`

## Requirement Status

- Complete: Verified the current ordering defect before implementation.
  Evidence: the updated focused regression failed with events ordered
  `["agent", "repair"]`, and the fail-closed test showed the adapter was still
  called before the mirror repair failure surfaced.
- Complete: Repair the shared mirror hooks path before launching the validation
  fix agent.
  Evidence: `src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py`
  now calls `repair_mirror_hooks_path` before `self._deps.adapter.run`.
- Complete: Preserve the post-agent mirror hooks repair as a race check.
  Evidence: the existing post-agent repair block remains in
  `_run_pre_push_validation_fix_pass`.
- Complete: Fail closed with `MIRROR_HOOKS_PATH_POISONED` if pre-launch repair
  fails, without launching the fix agent.
  Evidence: `test_pre_push_validation_fix_pass_fails_closed_on_git_mirror_hooks_repair_failure`
  now asserts `adapter.calls == []`.
- Complete: Add focused regression coverage for the pre-launch ordering and
  post-run check.
  Evidence:
  `test_pre_push_validation_fix_pass_repairs_hooks_path_before_and_after_agent`
  asserts the first repair precedes the agent launch and at least two repairs
  occur.
- Complete: Run only targeted local checks.
  Evidence: full AWF/GitHub validation was not run locally; it remains managed
  by AWF after agent completion.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k "hooks_path or mirror_hooks_repair_failure"`
  - First run before implementation: failed as expected.
  - Final run: `2 passed, 16 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`
  - Result: `All checks passed!`

## Remaining Gaps

None.
