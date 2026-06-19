# Fix staged conformance-report residue after base_commit restore

## Problem

`src/awf/control/executor/planning_ops.py:404-447` tries to remove a satisfied
post-validation conformance report from the worktree before push. The code:

1. Runs `git restore --source=base_commit --worktree --staged <report_path>`
2. If the command exits 0 but the path is still dirty relative to HEAD, logs a
   warning.
3. Falls back to `unlink()` of the worktree file.

GitHub review thread `PRRT_kwDOSJAM6s6KKSZU` points out that when the current
HEAD contains a version of the report that differs from `base_commit`
(e.g. an earlier fix pass committed the AWF-authored report), the restore
leaves a **staged** modification or deletion relative to HEAD. `unlink()`
removes the worktree copy but does **not** unstage the change. On the executor
push path, the staged/committed residue can still publish the stale
AWF-authored report commit.

## Requirements

1. After a successful `git restore --source=base_commit`, if the report path is
   still dirty relative to HEAD, restore the report path from **HEAD**
   (`--worktree --staged`) so both the index and worktree match HEAD, then
   verify the path is clean.
2. If the HEAD restore itself fails or the path remains dirty, fall back to the
   existing `unlink()` path and log as before.
3. Preserve the existing tracked/untracked behavior:
   * tracked report + `base_commit` restore clean  -> leave file on disk
   * tracked report + `base_commit` restore dirty  -> restore from HEAD
   * untracked report / restore fails             -> `unlink()`
4. Update/add regression tests covering staged-modification and
   staged-deletion residue.
5. Run focused unit tests, lint, and type checks only.

## Implementation Steps

1. Edit `_run_post_validation_conformance_check` in
   `src/awf/control/executor/planning_ops.py`:
   * Rename/logic: when `restore_result.ok` but worktree still dirty, attempt a
     second `git restore --source=HEAD --worktree --staged -- <report_path>`
     instead of immediately warning + unlinking.
   * If the HEAD restore succeeds and `_report_path_is_dirty` returns False,
     the path is now clean; return None.
   * If the HEAD restore fails or the path is still dirty after it, log the
     existing `restore_left_dirty` warning and fall back to `unlink()`.
   * Keep the existing untracked/restore-failure `unlink()` branch untouched.
2. Update `tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py`:
   * Adjust the `_GitRestoreFakeRunner` if needed.
   * Update the existing regression test that simulates a successful restore
     leaving a dirty path: it should now expect an additional
     `restore --source=HEAD ...` command and the report file should match HEAD
     content (stale committed report left on disk, not unlinked).
   * Add a new test for the staged-deletion residue case (file absent in
     `base_commit`, present in HEAD). After `base_commit` restore the status
     shows `D  report.txt`; the HEAD restore should recreate the HEAD copy and
     leave the tree clean.
3. Add a focused regression test in
   `tests/unit/control/test_planning_ops_branch_edges.py` covering the
   new helper/logic branches.
4. Run focused checks.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_002.py tests/unit/control/test_planning_ops_branch_edges.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py`

(Whole-repository validation is owned by AWF/GitHub CI after agent completion;
do not run full suites.)

## Out of Scope

- Refactoring other executor paths.
- Changing the base_commit restore source for tracked files.
- Adding full-coverage gates; follow the existing test-first focused coverage
  discipline.
