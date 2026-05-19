# Review Comment 4322870719 Plan

## Problem Statement and Scope

The review-level comment for PR #268 summarizes three concerns: missing git
configuration arguments in the PR monitor, duplicated protected-path helper
logic, and an unused workflow classification parameter. Local history already
contains targeted fixes for the duplicated helper and unused parameter. Source
inspection still shows PR monitor git commands built as `git -C ...` without
the safe-directory config helper.

Scope is limited to hardening the remaining PR monitor git worktree commands
and adding a regression test that prevents reintroducing direct `git -C`
command construction in `pr_monitor_runner.py`.

## Requirements Checklist

- Add a failing regression test before implementation.
- Ensure PR monitor worktree git commands include `git_safe_directory_config_args`.
- Preserve existing protected quality-gate semantic classification behavior.
- Do not change branch, push, or alter unrelated files.
- Run the narrowest relevant runtime tests after implementation.

## Implementation Steps

1. Add a PR monitor regression test that fails while direct `git -C` command
   construction remains in `src/awf/runtime/pr_monitor_runner.py`.
2. Introduce or reuse a single PR monitor helper for git worktree commands that
   injects safe-directory config args.
3. Replace remaining PR monitor `git -C` command arrays with the helper.
4. Run focused tests for PR monitor runner behavior and quality-gate
   classification.
5. Record validation results in `plans/REVIEW_COMMENT_4322870719_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py::test_pr_monitor_runner_git_worktree_commands_use_safe_directory_helper -q`
  - Passes after implementation and fails before it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges.py tests/unit/control/test_quality_gates.py -q`
  - Passes with no regressions in touched runtime and quality-gate behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner.py tests/unit/runtime/test_pr_monitor_runner.py`
  - Passes without lint errors.
