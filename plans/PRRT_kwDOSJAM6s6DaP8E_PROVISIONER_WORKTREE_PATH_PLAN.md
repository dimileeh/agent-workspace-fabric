# PRRT_kwDOSJAM6s6DaP8E Provisioner Worktree Path Plan

## Problem Statement and Scope

The PR review flagged `ControlWorker._preserved_active_worktree_path` for
reaching through `Provisioner._git._worktrees_dir`. The fix should expose a
public worktree-path contract and keep preserved-active execution recovery
behavior unchanged.

## Requirements Checklist

- Add a regression test showing `ControlWorker` resolves preserved-active
  worktree paths through a public provisioner method, without requiring private
  `_git` internals.
- Add a public worktree-path method on the provisioner boundary.
- Add a public worktree-path method on the git manager boundary so the
  provisioner does not depend on git-manager private storage.
- Remove private `_git` / `_worktrees_dir` access from `ControlWorker`.
- Keep validation scoped to the changed worker/provisioner behavior.

## Implementation Steps

1. Add the failing worker regression test.
2. Define the public provisioner worktree-path protocol in `ControlWorker`.
3. Implement `Provisioner.get_worktree_path`.
4. Implement `GitManager.get_worktree_path`.
5. Replace the private-attribute lookup in `ControlWorker`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_coverage_edges.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q`
  passes or any remaining failures are unrelated and documented.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_worker_coverage_edges.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes.
