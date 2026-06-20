# PRRT_kwDOSJAM6s6K-RQf Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K-RQf_PLAN.md`

## Requirement Status

- Verify `operation_start_head` exists before attempting branch-ref restoration:
  Complete. The existing `cat-file -e` guard remains before the moved
  `update-ref`.
- Only restore the branch ref after the worktree branch ref matches the expected
  workspace branch ref: Complete. The branch mismatch guard still returns before
  `update-ref`.
- When `MERGE_HEAD` is present, attempt to update the verified branch ref back
  to `operation_start_head` before returning `None`: Complete. The `update-ref`
  now runs before the merge-in-progress early return, and the regression test
  asserts that command.
- Preserve fail-closed recovery behavior while a merge is in progress:
  Complete. The regression test still asserts no mixed reset or commit runs in
  this branch.
- Add/update focused regression coverage: Complete. The existing
  `test_recover_missing_head_object_fails_closed_during_merge` now covers the
  branch-ref restoration behavior.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`
- `plans/PRRT_kwDOSJAM6s6K-RQf_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K-RQf_VALIDATION.md`

Focused verification:

- Initial red run after updating the test:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_during_merge -q`
  failed because no `update-ref` ran before the merge-state early return.
- Final green run:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_during_merge tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_updates_expected_branch_ref -q`
  passed with 2 tests.

Full AWF/GitHub validation was not run in the agent phase. AWF owns broad
validation, provenance, logs, and merge gating after completion.
