# Protected Scope Repair Head Verification Plan

## Problem Statement and Scope

An unresolved PR review thread reports that protected-scope repair can run an agent that self-commits with private object lookup state, then handles provider errors or runs `git status` before verifying the resulting `HEAD` object exists in the canonical mirror. If `HEAD` is missing, those branches can skip the existing missing-HEAD recovery/failure path and leave the shared mirror poisoned.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py` and focused unit coverage for `_repair_protected_scope_changes_before_commit`.

## Requirements Checklist

- Add a regression test that fails before the implementation by proving post-agent protected-scope repair verifies `HEAD` before provider-error handling.
- Add a regression test or assertion that status-based decisions are also gated by the same `HEAD` verification.
- Reuse the existing missing-HEAD error path; do not introduce broad new recovery behavior.
- Keep changes minimal and avoid protected workflow/config changes.
- Run targeted tests only; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add focused unit coverage in the existing protected-scope repair test file.
2. Update protected-scope repair to call `verify_head_object_exists(worktree_path)` after post-agent mirror cleanup and before provider-error or status handling.
3. Raise `_MonitorHeadObjectMissingError` with the existing unrecoverable reason code if the guard fails.
4. Run the new focused tests, then the containing test file if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::<new-test> -q`
  - Passes after implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_029.py -q`
  - Existing protected-scope repair coverage remains green.
