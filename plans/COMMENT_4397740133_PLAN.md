# Comment 4397740133 Remediation Plan

## Problem statement and scope
A review-level finding indicates validation-worktree dirty-state handling is still incomplete for ignored artifacts during pre-push validation cleanup. Current status checks include ignored paths (`--ignored=matching`), but ignored entries are not treated as untracked for cleanup, so cleanup may attempt `git restore` instead of `git clean` and fail.

Scope is intentionally limited to:
- Dirty-path parsing in `validation_worktree.py` for pre-push validation worktrees.
- Regression tests covering ignored-path detection and cleanup behavior.

Out of scope:
- Other git status parsing entry points outside validation cleanup.
- Broad runtime behavior changes for non-validation flows.

## Requirements checklist
- [ ] Treat `!!` paths from validation-status output as untracked cleanup targets in `check_validation_worktree_clean`.
- [ ] Keep cleanup behavior consistent with existing `-fdx` hardening for ignored/untracked removal.
- [ ] Add/adjust unit tests that assert ignored dirt is captured in both `paths` and `untracked_paths` and is cleaned via `git clean`.
- [ ] Preserve existing handling for tracked dirty files and HEAD verification.

## Implementation steps
1. Update shared porcelain parser in `src/awf/runtime/pr_monitor_runner/path_parsing.py`:
   - Include ignored entries (`!! ...`) in `_untracked_paths_from_porcelain`.
   - Add ignored-entry handling in `_untracked_paths_from_porcelain_z` for completeness.
2. Update existing focused unit tests in `tests/unit/runtime/test_validation_worktree.py` if needed for regression stability.
3. Add a focused regression test in `tests/unit/runtime/` for a mixed tracked + ignored cleanup path where ignored artifacts are removed by `git clean`.
4. Verify no unintended changes outside the targeted files.

## Verification commands and pass criteria
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_007.py -q`

Pass criteria:
- Ignored-path tests pass and explicitly include ignored entries in untracked extraction where `--ignored` is used.
- No behavioral change in paths not using ignored status lines.
