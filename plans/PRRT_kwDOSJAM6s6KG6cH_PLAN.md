# Plan: Restore tracked conformance reports instead of staging deletion

Thread: `PRRT_kwDOSJAM6s6KG6cH`
File: `src/awf/control/executor/planning_ops.py` (line ~406)

## Problem statement

The post-validation conformance report cleanup in `_run_post_validation_conformance_check` currently does:

1. `git rm -- <report_path>` for tracked reports.
2. If that fails (untracked / gitignored / modified tracked file), fall back to `unlink`.

`git rm` of a tracked file stages the deletion (`D  ...`), and a tracked file with local modifications makes `git rm` fail, which falls back to `unlink`, leaving an unstaged deletion (` D ...`). Both cases still leave a porcelain entry, so `check_validation_worktree_clean()` and the PR monitor pre-push guard see a dirty worktree.

The desired behavior is to restore the report path to its state in the index (i.e. the tracked content, if any), and then remove the on-worktree copy, so the worktree is actually clean for tracked projects. For untracked/gitignored reports we keep a plain `unlink` fallback.

## Requirements checklist

- [ ] For a tracked conformance report, the worktree ends clean (no staged or unstaged deletion) after the cleanup.
- [ ] For an untracked/gitignored conformance report, the on-worktree file is still removed.
- [ ] Existing regression tests for tracked/untracked cases are updated or replaced to assert the new clean behavior.
- [ ] No `git add` or `git commit` runs for the AWF artifact.
- [ ] Coverage is preserved or improved; new code paths covered.
- [ ] Focused test suite passes (`test_planning_ops_branch_edges.py` + `test_executor_coverage_edges_part_001.py` conformance tests + lint).

## Implementation steps

1. In `src/awf/control/executor/planning_ops.py`, replace the `git rm` logic with:
   - First attempt `git restore --source=HEAD --worktree --staged -- <report_path>` to put the index/worktree back to the committed state.
   - Then remove the on-worktree copy with `unlink`.
   - If `git restore` fails (e.g. path is not tracked), fall back to plain `unlink` and log.
2. Update the docstring / inline comment to explain why we restore instead of stage-delete.
3. Update test doubles that simulate git behavior:
   - `_GitRmFakeRunner` in `test_executor_coverage_edges_part_001.py` to simulate `git restore` and unlink instead of `git rm`.
4. Fix the stale test in `test_planning_ops_branch_edges.py` that expects a `git restore` result to be consumed (adjust queued results and assertions).
5. Update or add a regression test asserting a tracked report leaves the worktree clean (no porcelain change from `git status`).
6. Run focused tests and lint.

## Verification commands and pass criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_planning_ops_branch_edges.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/planning_ops.py tests/unit/control/test_planning_ops_branch_edges.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_001.py
uv run --python 3.12 --extra dev mypy src/awf/control/executor/planning_ops.py
```

Pass criteria: all four commands succeed without errors.
