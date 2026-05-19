# Review 4482045018 Partial Write and Duplicate Context Plan

## Problem Statement and Scope

Address two review-level edge cases in compose env seeding:

- `_seed_env_file` can leave an empty or partial env file behind when exclusive
  creation succeeds but `write()` raises `OSError`.
- `_merge_env_seed_contents_with_overlay_keys` drops non-assignment context that
  appears before the first occurrence of a duplicate overlay key.

Scope is limited to the env seeding implementation, focused regression tests,
and validation evidence for this review comment.

## Requirements Checklist

- [x] Add a regression test proving `_seed_env_file` removes a partial env file
  after a mid-write failure and reports `write_failed`.
- [x] Add a regression test proving duplicate overlay keys retain context before
  the first occurrence when the final value is merged.
- [x] Implement the smallest code changes that satisfy the tests without
  weakening existing env merge behavior.
- [x] Run targeted unit tests for the changed behavior.
- [x] Commit the scoped changes locally with a conventional commit message.

## Implementation Steps

1. Add failing tests in `tests/unit/cli/test_init.py` for the partial write and
   duplicate first-context cases.
2. Run the new tests to confirm they fail against the current implementation.
3. Update `src/awf/cli/main.py`:
   - Clean up the newly created env file if `write()` fails after `open("xb")`.
   - Preserve duplicate-key context by carrying context from non-final
     occurrences forward to the final occurrence.
4. Re-run the targeted tests.
5. Create the validation document with requirement status and command evidence.
6. Stage only changed files and commit locally.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_init.py::test_seed_env_file_removes_partial_file_after_write_failure \
  tests/unit/cli/test_init.py::test_merge_env_seed_contents_preserves_context_before_first_duplicate_overlay_key \
  -q
```

Pass criteria: both targeted tests fail before the implementation change and
pass after the implementation change.
