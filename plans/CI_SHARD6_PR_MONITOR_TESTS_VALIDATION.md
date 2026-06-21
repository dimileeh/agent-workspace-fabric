# CI shard 6 PR-monitor tests validation

Plan reference: `plans/CI_SHARD6_PR_MONITOR_TESTS_PLAN.md`

## Requirement status

- Complete: Preserved AWF branch ownership. No branch switch, push, rebase, or
  broad AWF/GitHub validation was run.
- Complete: Reproduced the nine shard-6 failures locally before edits.
- Complete: Kept production changes minimal and tied to a real missing ownership
  guard before protected-scope repair agent launch.
- Complete: Updated focused fakes for current mirror-anchor checks, sanitized
  env plumbing, preserved-head capture, and settle-poll behavior.
- Complete: Preserved reason-code assertions for protected-scope and ownership
  failures.
- Complete: Ran focused shard-6 pytest targets and Ruff.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
- `tests/unit/runtime/test_pr_monitor_pre_push_validation_edges.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_014.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_015.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`

Commands run:

- Focused nine-test shard-6 pytest command from CI failures.
  - Before: `9 failed`.
  - After: `9 passed`.
- `uv run --python 3.12 --extra dev ruff check <touched source/test files>`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/remote_ops.py src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
  - Result: passed.

## Residual risk

The full shard matrix and coverage merge are left to AWF/GitHub CI after agent
completion.
