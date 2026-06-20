# Protected Repair HEAD Guard Validation

Plan reference: `plans/PROTECTED_REPAIR_HEAD_GUARD_PLAN.md`

## Requirement Status

- Unexpected non-`AgentRunError` adapter exceptions run the same post-adapter
  `HEAD` object guard before being re-raised: Complete.
  - Evidence: `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
    now calls the extracted `_verify_repair_head_object_exists` helper in the
    unexpected-exception path after mirror hooks repair.
- Existing mirror `core.hooksPath` repair behavior is preserved: Complete.
  - Evidence: the exception path still calls `_repair_recovery_mirror_hooks_path`
    before any `HEAD` verification.
- Existing `AgentRunError` and successful repair behavior is preserved:
  Complete.
  - Evidence: the original post-adapter guard was moved into a local helper and
    is still called after normal adapter completion/`AgentRunError` capture.
- Changes remain minimal and broad validation is left to AWF: Complete.
  - Evidence: only the targeted production module, focused unit test file, and
    plan/validation artifacts changed. Full AWF/GitHub validation was not run.

## Verification

- Failing-before evidence:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_protected_scope_repair_checks_head_after_unexpected_adapter_failure -q`
  - Failed with `RuntimeError: compose cleanup failed`, showing the missing
    `HEAD` guard on the unexpected adapter exception path.
- Passing-after evidence:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_protected_scope_repair_checks_head_after_unexpected_adapter_failure -q`
  - Result: `1 passed`.
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py -q`
  - Result: `16 passed`.
- Targeted lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py`
  - Result: `All checks passed!`.

No gaps remain.
