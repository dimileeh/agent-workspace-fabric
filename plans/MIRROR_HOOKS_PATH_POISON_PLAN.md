# Mirror Hooks Path Poison Plan

## Problem Statement and Scope

An unresolved PR review thread reports that `repair_mirror_hooks_path` clears every
`core.hooksPath` value from a shared bare mirror, including legitimate profile or
project hook directories. The fix is scoped to preserving non-poisoned
`core.hooksPath` entries while still removing known hook-disabling poison values.

## Requirements Checklist

- Confirm the review against the current implementation.
- Add focused regression coverage for legitimate hook paths.
- Remove only known poisoned `core.hooksPath` values.
- Preserve current success and error handling for missing config, concurrent
  cleanup, and repair failures.
- Run only focused tests for the changed behavior; broad AWF/GitHub validation is
  handled after agent completion.

## Implementation Steps

1. Add tests in `tests/unit/node/test_git_manager.py` proving legitimate hook
   paths are preserved, including when a poison value is also present.
2. Update `src/awf/node/git_manager.py` to inspect all local `core.hooksPath`
   values and unset only exact known poison values.
3. Keep the existing reason code and operation naming style for failures.
4. Run the focused test selection for `TestRepairMirrorHooksPath`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager.py::TestRepairMirrorHooksPath -q`
  passes after implementation.
- The same focused selection fails before implementation on the new regression
  when practical to run.
