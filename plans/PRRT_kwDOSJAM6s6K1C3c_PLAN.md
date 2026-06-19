# PRRT_kwDOSJAM6s6K1C3c Plan

## Problem Statement And Scope

The PR monitor missing-HEAD recovery path strips Git object lookup environment overrides for some verification commands, but review feedback reports that recovery writes still inherit ambient `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES`.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_repair.py` and focused regression coverage for `_recover_missing_head_object_from_filesystem`.

## Requirements Checklist

- Verify whether the review claim is actionable against the current code.
- Ensure missing-HEAD recovery git writes do not inherit object lookup override environment variables.
- Cover the changed recovery behavior with a focused unit regression.
- Run only targeted validation for the touched behavior; leave broad AWF/GitHub validation to AWF after agent completion.

## Implementation Steps

1. Add a regression test that sets object lookup override env vars and asserts recovery write commands run with sanitized env.
2. Update recovery git command helpers/direct commit call to use `git_env_without_object_lookup_overrides()` for recovery git operations.
3. Run the focused test file or specific test selection covering the new regression.
4. Record validation evidence in `plans/PRRT_kwDOSJAM6s6K1C3c_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_fails_closed_on_branch_ref_mismatch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_sanitizes_recovery_write_env tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py::test_recover_missing_head_object_verifies_final_head_in_mirror -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_019.py`

Pass criteria: the targeted env-sanitization tests pass, including the new regression, and focused lint passes for touched files. Full AWF/GitHub validation is intentionally not run in this agent phase.

## Assumptions/Changes

The full `test_pr_monitor_runner_coverage_edges_part_019.py` file was attempted and exposed unrelated failures around existing fake command outputs/recovery-anchor setup. Those failures are outside this review thread, so validation is scoped to the env-sanitization behavior changed here.
