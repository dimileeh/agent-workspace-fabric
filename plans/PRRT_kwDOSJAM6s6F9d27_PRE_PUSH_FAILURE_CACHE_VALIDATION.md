# PRRT_kwDOSJAM6s6F9d27 Pre-Push Failure Cache Validation

## Plan Alignment

- Added a focused regression test in
  `tests/unit/runtime/test_pr_monitor_pre_push_validation.py` that instruments
  `_failed_pre_push_commands` and asserts a single traversal during one
  `_run_pre_push_validation` reason-code decision.
- Refactored `src/awf/runtime/pr_monitor_runner/pre_push_validation.py` so
  failed commands are collected once and passed through helper functions for
  preferred failure, pure toolchain-missing detection, and validation reason
  derivation.
- Preserved existing behavior for pure toolchain-missing failures, mixed
  command-not-found plus real failures, and coverage policy failures.

## Focused TDD Evidence

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -k failed_commands_once -q`
  failed with `assert 3 == 1`.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -k failed_commands_once -q`
  passed with `1 passed, 20 deselected`.
- Focused behavior check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_pre_push_validation.py -k "failed_commands_once or toolchain_missing_bypasses_fix_pass or mixed_127_prefers_real_failure_for_fix_pass or coverage_failure_persists_coverage_reason_code" -q`
  passed with `4 passed, 17 deselected`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
  passed.
- Focused typing:
  `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
  passed.

## Gaps

Full AWF/GitHub validation was not run during the agent phase per the workspace
contract. AWF owns broad validation, provenance, and merge gating after agent
completion.
