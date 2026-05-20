# PRRT_kwDOSJAM6s6DaP8E Provisioner Worktree Path Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DaP8E_PROVISIONER_WORKTREE_PATH_PLAN.md`

## Requirement Status

- Add a regression test showing `ControlWorker` resolves preserved-active
  worktree paths through a public provisioner method: Complete.
  - Evidence: `test_preserved_active_worktree_path_uses_public_provisioner_method`
    initially failed because the worker returned `None`; it passes after the
    implementation.
- Add a public worktree-path method on the provisioner boundary: Complete.
  - Evidence: `Provisioner.get_worktree_path`.
- Add a public worktree-path method on the git manager boundary: Complete.
  - Evidence: `GitManager.get_worktree_path`.
- Remove private `_git` / `_worktrees_dir` access from `ControlWorker`:
  Complete.
  - Evidence: `_preserved_active_worktree_path` now calls the provisioner
    public method.
- Keep validation scoped to the changed worker/provisioner behavior: Complete.

## Verification Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py::test_preserved_active_worktree_path_uses_public_provisioner_method -q`
  - Failed before implementation with `None` returned from the private-lookup path.
  - Passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q`
  - Passed: 49 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  - Passed: 198 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_worker_coverage_edges.py tests/unit/control/test_worker.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

## Gaps

None.
