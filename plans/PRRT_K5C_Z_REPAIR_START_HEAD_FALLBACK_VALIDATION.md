# PRRT K5C Z Repair Start Head Fallback Validation

Plan reference:
`plans/PRRT_K5C_Z_REPAIR_START_HEAD_FALLBACK_PLAN.md`

## Requirement Status

- Verify the review claim against `remote_repair.py` and cited callers:
  Complete. `_repair_operation_start_head_result` only used
  `fallback_head_sha` while the worktree was missing; cited sync-base and CI
  repair callers pass their PR head through that parameter.
- Add a focused regression test before implementation: Complete.
  `test_repair_operation_start_head_uses_fallback_when_rev_parse_fails` failed
  before the helper change because the returned head was empty.
- Use `fallback_head_sha` when worktree `rev-parse HEAD` fails: Complete.
  The helper now returns the supplied fallback head before producing
  `REPAIR_START_HEAD_UNAVAILABLE`.
- Preserve existing behavior that prefers a successful worktree HEAD: Complete.
  The successful `rev-parse HEAD` return path remains unchanged.
- Keep changes scoped to the review feedback: Complete.
  Only the shared helper, focused unit test, and plan/validation docs changed.
- Run only targeted validation for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `plans/PRRT_K5C_Z_REPAIR_START_HEAD_FALLBACK_PLAN.md`
- `plans/PRRT_K5C_Z_REPAIR_START_HEAD_FALLBACK_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k repair_operation_start_head`
  - Before implementation: failed on the new fallback regression.
  - After implementation: `3 passed, 18 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
  - Result: `All checks passed!`

Full AWF/GitHub validation is managed by AWF after agent completion and was not
run in this agent phase.
