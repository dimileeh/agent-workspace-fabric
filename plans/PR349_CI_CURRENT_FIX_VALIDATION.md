# PR349 CI Current Fix Validation

Plan reference: `plans/PR349_CI_CURRENT_FIX_PLAN.md`

## Requirement Status

- Keep work on current AWF branch; do not push/rebase/switch: Complete.
- Avoid protected workflow/quality-gate configuration edits: Complete.
- Reproduce observed CI failures with focused commands: Complete.
- Fix real test regressions without disabling checks: Complete.
- Run focused verification and leave broad validation to AWF/GitHub: Complete.
- Commit the local fix: Complete.

## Evidence

CI inspection:

- `gh pr view 349 --repo dimileeh/aira-agent-workspace-fabric --json ...`
  showed PR head `ad455aed7b56b0ba46e5b15674e16bd20cfed881`.
- `gh run view 26728322700 --repo dimileeh/aira-agent-workspace-fabric`
  showed the completed failure was `python-full-coverage`; `ci-required`
  failed only because that required job failed.
- `gh pr checks 349 --repo dimileeh/aira-agent-workspace-fabric` after local
  fixes showed `lint-and-type`, `console`, `release-artifacts`, Cursor, and
  CodeRabbit passing; `python-full-coverage` and Greptile were still pending on
  GitHub at the time of validation.

Focused local repro before fixes:

- Targeted failing node ids from the CI log: `7 failed, 3 passed`.

Focused local verification after fixes:

- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py tests/unit/control/test_executor_validation_fix_cycle_recovery.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`
  passed: `65 passed`.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
  passed: `56 passed`.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_fix_pass.py`
  passed: `49 passed`.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/control/test_executor_validation_fix_cycle.py tests/unit/control/test_executor_validation_fix_cycle_recovery.py`
  passed: `38 passed`.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_007.py`
  passed: `34 passed`.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest -q tests/unit/runtime/test_validation_worktree.py::test_cleanup_validation_worktree_cleans_new_ignored_files_using_snapshot`
  passed: `1 passed`.
- `uv run --python 3.12 --extra dev pytest -q <original failure node ids in their current files>`
  passed: `7 passed`.
- `uv run --python 3.12 --extra dev ruff check <changed test files>`
  passed.
- `git diff --check` passed.

## Residual Risk

Full `python-full-coverage` was not run locally per the AWF workspace contract.
AWF/GitHub owns the broad coverage gate and provenance after this agent phase.
