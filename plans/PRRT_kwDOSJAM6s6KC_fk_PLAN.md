# PRRT_kwDOSJAM6s6KC_fk Plan: Pass safe.directory in gitlink detection

## Problem statement

The review thread at `src/awf/runtime/validation_worktree.py:184` notes that
`_is_tracked_gitlink` runs a raw `git -C <worktree> ls-tree HEAD -- <path>`
without injecting Git's `safe.directory` override. In workspaces where Git
requires the override (e.g. mounted paths with mismatched ownership), this
call can fail with:

```
fatal: detected dubious ownership in repository at ...
```

When `_is_tracked_gitlink` returns `False` because of that failure, the
empty-directory cleanup may `rmdir` a deinitialized tracked submodule. The
directory is actually tracked as a `160000` gitlink in HEAD, so removing it
dirties an otherwise clean worktree before push.

## Scope

- Modify `_is_tracked_gitlink` in `src/awf/runtime/validation_worktree.py` so the
  internal `git ls-tree` call includes the same `-c safe.directory=<worktree>`
  injection used by `awf.runtime.pr_monitor_runner.git_utils.git_worktree_command`.
- Add a focused regression test in `tests/unit/runtime/test_validation_worktree.py`
  that proves the command includes `safe.directory`.
- Do not refactor broader worktree cleanup logic or unrelated git calls.

## Requirements checklist

1. `_is_tracked_gitlink` command vector includes `-c safe.directory=<worktree_path>`.
2. Existing submodule-preservation behavior is unchanged.
3. New regression test fails before the fix and passes after.
4. Existing tests in `tests/unit/runtime/test_validation_worktree.py` still pass.
5. `ruff check` and `mypy` pass for touched files.

## Implementation steps

1. Import `git_safe_directory_config_args` from `awf.common.git_identity` in
   `src/awf/runtime/validation_worktree.py`.
2. Update `_is_tracked_gitlink` to prepend the safe-directory config args before
   the existing `git -C <worktree> ls-tree ...` arguments.
3. Add a unit test that creates a real git worktree, adds a submodule, deinit
   it, then asserts the `ls-tree` command vector includes `safe.directory`.
   Use `monkeypatch` on `subprocess.run` to capture the constructed command.
4. Run targeted tests and checks.
5. Write `PRRT_kwDOSJAM6s6KC_fk_VALIDATION.md`.

## Verification commands

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation_worktree.py tests/unit/runtime/test_validation_worktree.py
uv run --python 3.12 --extra dev mypy src/awf/runtime/validation_worktree.py
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q
```

All must pass.
