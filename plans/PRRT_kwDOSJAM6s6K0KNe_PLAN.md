# PRRT_kwDOSJAM6s6K0KNe Plan

## Problem Statement and Scope

`verify_head_object_exists` checks `HEAD^{commit}` with `git cat-file`, but the subprocess currently inherits the ambient process environment. If `GIT_OBJECT_DIRECTORY` or `GIT_ALTERNATE_OBJECT_DIRECTORIES` is set by a recovery path, Git can resolve `HEAD` through unrelated object stores and report a missing local mirror object as present.

Scope is limited to the PR review thread on `src/awf/node/git_manager.py`.

## Requirements Checklist

- Add a focused regression test proving `verify_head_object_exists` ignores inherited Git object lookup environment variables.
- Ensure the `git cat-file` subprocess used by `verify_head_object_exists` does not inherit `GIT_OBJECT_DIRECTORY` or `GIT_ALTERNATE_OBJECT_DIRECTORIES`.
- Keep the change minimal and avoid unrelated Git manager refactors.
- Run only targeted validation for the changed behavior; full AWF/GitHub validation remains managed after agent completion.

## Implementation Steps

1. Add a failing unit test that creates a worktree whose branch ref points at a commit object available only through an inherited object environment.
2. Add a small environment-sanitizing helper for the `verify_head_object_exists` subprocess.
3. Pass the sanitized environment to `asyncio.create_subprocess_exec`.
4. Run the targeted unit test for `TestVerifyHeadObjectExists`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestVerifyHeadObjectExists -q`

Pass criteria: the focused test class passes, including the new regression.
