# PR #606 coverage repair plan

## Problem

GitHub CI job `python-full-coverage` failed on PR #606 (run 27666319065). The
combined line+branch coverage was `77256/78042 = 98.99 %`, just below the
required 99 %.

The missing coverage units are concentrated in the files already owned by this
repair agent:

- `src/awf/runtime/validation_worktree.py`: 21 uncovered lines and 9 uncovered
  branches.
- `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`: 2 uncovered lines
  and 4 uncovered branches.

Other files have pre-existing gaps, but closing only the owned gaps is enough to
push the combined total over the 99 % threshold.

## Requirements

1. Do not lower, skip, or weaken the coverage gate.
2. Only change owned paths listed in the workspace contract:
   - `src/awf/runtime/validation_worktree.py`
   - `src/awf/runtime/pr_monitor_runner/pre_push_validation.py`
   - `tests/unit/runtime/test_validation_worktree.py`
   - `tests/unit/runtime/test_pr_monitor_pre_push_validation.py`
   - `tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py`
3. Add real behavior-covering tests; do not write coverage-theater tests.
4. For genuinely unreachable defensive code, use a justified `# pragma: no cover`
   instead of a hollow test.
5. Keep changes minimal and obviously correct.
6. Run focused unit tests and lint/type checks only; leave the full CI suite to
   AWF/GitHub.

## Root-cause gaps

### `validation_worktree.py`

- `_collapse_descendant_cleanup_paths` branch where a path is **kept** because
  an ancestor appears later in the input list is uncovered (branch 78 -> 83).
- `_remove_empty_untracked_dirs` contains two redundant checks at the rmdir
  site (lines 296-300). Because child directories are already treated as
  boundaries earlier in the same function, these checks are unreachable in
  current code. They were carried over from the per-call `git ls-tree` probe and
  now only create uncovered surface area.
- `_gitlink_paths` has a defensive parse guard for a `160000` entry without a
  tab. Real `git ls-tree -z` output always includes a tab, so this branch is
  unreachable.

### `pre_push_validation.py`

- `_run_pre_push_validation_fix_pass` exception handlers return
  `True, rollback_failure_reason` when the recovery rollback itself fails. No
  test currently exercises a failed recovery after an agent/compose/commit
  exception.

## Implementation steps

1. `validation_worktree.py`
   - Remove the two redundant rmdir-site checks (lines 296-300). They are
     already enforced by the boundary check at line 281 and have no effect.
   - Add a justified `# pragma: no cover` on the defensive tab-not-found branch
     inside `_gitlink_paths`.
2. `test_validation_worktree.py`
   - Add a focused test for `_collapse_descendant_cleanup_paths` that exercises
     the case where an ancestor appears after its descendants.
3. `test_pr_monitor_pre_push_validation.py`
   - Add a regression test where a fix-pass agent raises `AgentRunError` and
     the subsequent worktree rollback fails, covering the
     `return True, rollback_failure_reason` path.
4. Run focused verification:
   - `uv run --python 3.12 --extra dev ruff check src/awf tests`
   - `uv run --python 3.12 --extra dev mypy src/awf`
   - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py tests/unit/runtime/test_pr_monitor_pre_push_validation.py tests/unit/runtime/test_pr_monitor_pre_push_validation_cleanup.py -q`
5. Re-compute local coverage for the touched files to confirm the owned gaps
   are closed.
6. Write `plans/PR606_COVERAGE_VALIDATION.md` and commit.
