# REVIEW_PRRT_kwDOSJAM6s6K-aI0 Recovered HEAD Ownership Validation

Plan reference:
`plans/REVIEW_PRRT_KWDOSJAM6S6K_AI0_RECOVERED_HEAD_OWNERSHIP_PLAN.md`

## Requirement Status

- Add a focused regression test for recovered missing-HEAD ownership failure in
  `_commit_dirty_worktree`: Complete.
- Ensure the recovered delta is cleaned back to `recovery_head` before raising
  `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED`: Complete.
- Preserve the existing protected-scope cleanup behavior: Complete; the change
  reuses `_cleanup_recovered_missing_head_delta` and does not alter the existing
  protected-scope branch.
- Run only focused validation for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`

Focused test-first evidence:

- Before implementation, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k recovered_head_ownership`
  failed because only the recovered diff command ran and no `reset --hard
  <recovery_head>` command was issued.

Focused validation after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k recovered_head_ownership`
  passed: `1 passed, 21 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  passed.

Full AWF/GitHub validation was not run in this agent phase; AWF owns broad
validation, provenance, logs, and merge gating after agent completion.
