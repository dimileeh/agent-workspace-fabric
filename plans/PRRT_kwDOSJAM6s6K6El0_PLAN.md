# PRRT_kwDOSJAM6s6K6El0 Push Git Env Plan

## Problem Statement And Scope

The PR monitor push path repairs mirror hooks before publishing, but the actual
`git push` and non-fast-forward resync `git fetch` / `git reset --hard` calls can
still inherit `GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES` from
the monitor process. That can make push/recovery observe or write through a
private object store instead of the workspace mirror.

Scope is limited to `_git_push_result` in `remote_ops.py` and focused regression
coverage for that push/resync path.

## Requirements Checklist

- Strip Git object lookup override environment from the `git push` publish call.
- Strip the same environment from non-fast-forward resync `git fetch`.
- Strip the same environment from non-fast-forward resync `git reset --hard`.
- Add focused regression coverage proving inherited object env keys are absent
  from all three commands while unrelated env is preserved.
- Run targeted local validation only; broad AWF/GitHub validation remains managed
  after agent completion.

## Implementation Steps

1. Add a focused failing test around `_git_push_result` rejected-push recovery.
2. Pass `env=git_env_without_object_lookup_overrides()` to push, resync fetch,
   and resync reset runner calls.
3. Run the targeted pytest selection and a focused ruff check for changed files.
