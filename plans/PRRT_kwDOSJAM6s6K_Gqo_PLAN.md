# PRRT_kwDOSJAM6s6K_Gqo Plan

## Problem Statement

Review thread `PRRT_kwDOSJAM6s6K_Gqo` reports that
`verify_head_object_exists()` treats any repository-level
`objects/info/alternates` file as missing HEAD without clearing the poison.
That can wedge every linked worktree backed by the shared mirror even when the
HEAD object is physically present in the mirror.

Scope is limited to HEAD-object verification alternates handling in
`src/awf/node/git_manager.py` and focused unit coverage in
`tests/unit/node/test_git_manager_head_object.py`.

## Requirements

- Clear repository-level alternates before running the HEAD object probe.
- Preserve fail-closed behavior when alternates are the only reason an object
  appears reachable.
- Fail closed if AWF cannot remove the alternates file.
- Keep the change focused to this review thread.

## Implementation Steps

1. Add a regression test for a valid mirror HEAD with a stray alternates file.
2. Update the existing repository-alternates test to assert the poison file is
   cleared while the alternate-only object remains rejected.
3. Replace the early alternates rejection with a cleanup helper that removes the
   alternates file before `git cat-file`.
4. Run the focused regression tests and a targeted lint check for touched files.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_head_object.py -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/node/git_manager.py tests/unit/node/test_git_manager_head_object.py`

Full AWF/GitHub validation is managed by AWF after agent completion.
