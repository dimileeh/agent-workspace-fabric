# Review 4508578544 Threaded Ownership Repair Plan

## Problem Statement And Scope

Follow-up review feedback for PR comment `issue:4508578544` notes that
`repair_agent_runtime_ownership` still performs linked-worktree validation
filesystem reads on the event loop before offloading the chown repair work.

Scope is limited to moving validation and repair into one synchronous helper
that runs under `asyncio.to_thread`, while preserving existing failure logging
and validated linked-git-dir propagation.

## Requirements Checklist

- Add regression coverage proving validation and repair execute inside the
  `asyncio.to_thread` handoff.
- Move `_validated_layout_mirror_for_worktree` into the same threaded helper as
  `repair_agent_writable_worktree`.
- Preserve existing success return value and exception-to-structured-log failure
  behavior.
- Run targeted ownership tests and lint for touched files.
- Commit the scoped fix locally with a conventional commit message.

## Implementation Steps

1. Add a unit test that monkeypatches `asyncio.to_thread` and verifies both
   validation and repair execute during the threaded callback.
2. Run the targeted new test and confirm it fails before implementation.
3. Extract the validation plus repair sequence into a synchronous helper called
   from `asyncio.to_thread`.
4. Re-run the targeted test, the full ownership unit test file, ruff on touched
   files, and `git diff --check`.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py::test_repair_agent_runtime_ownership_runs_validation_inside_thread -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_ownership.py -q
uv run --python 3.12 --extra dev ruff check src/awf/runtime/ownership.py tests/unit/runtime/test_ownership.py
git diff --check
```

Pass criteria: the new regression fails before implementation, then all listed
commands pass after the implementation.
