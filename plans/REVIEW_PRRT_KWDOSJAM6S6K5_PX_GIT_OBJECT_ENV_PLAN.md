# Review PRRT_kwDOSJAM6s6K5-pX Git Object Env Plan

## Problem Statement And Scope

The dirty-worktree monitor commit path verifies HEAD with Git object lookup
overrides stripped, but later `git status`, `git add`, and `git commit`
commands can still inherit `GIT_OBJECT_DIRECTORY` or
`GIT_ALTERNATE_OBJECT_DIRECTORIES`. If inherited, those write commands can place
new commit objects outside the workspace mirror while updating the workspace ref.

Scope is limited to the dirty-worktree commit path and its immediate pre-commit
autofix retry helper.

## Requirements Checklist

- Strip Git object lookup override environment from dirty-worktree status reads.
- Strip the same environment from dirty-worktree staging, cached-diff, and commit
  write/read commands.
- Strip the same environment from the pre-commit autofix retry status, add, and
  commit commands.
- Add focused regression coverage proving inherited object environment keys are
  not passed to the dirty-worktree write path.
- Run only targeted validation for the changed monitor tests; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add `env=git_env_without_object_lookup_overrides()` to the relevant
   `remote_repair._commit_dirty_worktree` Git invocations.
2. Add the same sanitized env in `commit_autofix._retry_monitor_precommit_autofix_commit_once`.
3. Extend focused dirty-worktree monitor tests to assert the env keys are absent
   from status/add/diff/commit calls when inherited by the monitor process.
4. Run the targeted pytest selection covering the new and related dirty-worktree
   retry tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k "commit_dirty_worktree_strips_git_object_env_from_write_path or commit_dirty_worktree_restages_precommit_autofix_and_retries_commit"`
  - Passes without failures.
