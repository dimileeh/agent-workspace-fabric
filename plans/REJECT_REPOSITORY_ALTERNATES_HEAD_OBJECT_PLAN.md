# Reject Repository Alternates In HEAD Object Verification Plan

## Problem statement and scope

Review thread `PRRT_kwDOSJAM6s6K-T0S` reports that `verify_head_object_exists()` trusts `git cat-file -e HEAD^{commit}` even when the backing mirror repository has `objects/info/alternates` pointing to a workspace-private object store. That can make a HEAD commit appear present even when the shared mirror does not physically own the object.

Scope is limited to the shared HEAD object verification helper and focused unit coverage for the alternates bypass.

## Requirements checklist

- Add a regression test showing a mirror-local `objects/info/alternates` file can otherwise make a missing HEAD object pass verification.
- Make `verify_head_object_exists()` fail closed when the backing repository declares object alternates.
- Preserve existing behavior for valid HEADs, missing objects, and inherited object lookup environment variables.
- Run only focused checks for the changed behavior; broad AWF/GitHub validation remains managed after agent completion.

## Implementation steps

1. Add a focused test to `tests/unit/node/test_git_manager_head_object.py` that rewrites the worktree branch ref to a commit existing only in an alternate object store and writes that store to the mirror `objects/info/alternates` file.
2. Update `src/awf/node/git_manager.py` so `verify_head_object_exists()` rejects a backing repository with alternates before trusting `git cat-file`.
3. Run the focused test file.
4. Document validation evidence in `plans/REJECT_REPOSITORY_ALTERNATES_HEAD_OBJECT_VALIDATION.md`.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_head_object.py -q`
  - Passes all focused HEAD-object verification tests, including the new alternates regression.

Full AWF/GitHub validation is intentionally not run in the agent phase per workspace contract.
