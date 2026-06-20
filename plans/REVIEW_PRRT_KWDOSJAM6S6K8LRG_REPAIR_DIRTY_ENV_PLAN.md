# REVIEW_PRRT_KWDOSJAM6S6K8LRG Repair Dirty Env Plan

## Problem Statement and Scope

The repair-start dirty worktree guard runs `git status --porcelain --untracked-files=all`
before launching agent repair. Review feedback reports that this probe does not strip
`GIT_OBJECT_DIRECTORY` and `GIT_ALTERNATE_OBJECT_DIRECTORIES`, unlike other hardened
monitor git calls, so inherited object lookup overrides can skew the dirty check.

Scope is limited to the repair dirty guard in `remote_repair.py` and a focused
regression test for the command environment.

## Requirements Checklist

- Confirm whether the current dirty guard passes a sanitized git environment.
- If missing, pass `git_env_without_object_lookup_overrides()` to that `git status` call.
- Add a focused regression proving object lookup override variables are not inherited.
- Run only targeted tests for the changed behavior.
- Leave broad AWF/GitHub validation to the post-agent pipeline.

## Implementation Steps

1. Inspect the reported call site and existing hardened git call pattern.
2. Update the dirty guard status command to pass the sanitized environment.
3. Extend the existing repair guard tests with an environment-sanitization assertion.
4. Run the focused unit test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_agent_runtime_memory_repair_guard.py -q`
  - Passes with the new regression and existing repair guard tests.

Full repository validation, coverage gates, and GitHub merge checks are managed by AWF
after agent completion per the workspace contract.
