# PRRT_kwDOSJAM6s6K0ZNy Plan

## Scope

Address the review thread reporting that runner-based `git cat-file` commit
existence checks inherit `GIT_OBJECT_DIRECTORY` and
`GIT_ALTERNATE_OBJECT_DIRECTORIES`, unlike `verify_head_object_exists`.

## Steps

1. Add focused regression assertions for the affected PR monitor cat-file calls:
   fix-cycle per-item HEAD validation, stale operation-start validation, and
   mirror recovery start-head validation.
2. Reuse the existing git object-lookup environment sanitization for those
   runner invocations.
3. Run only targeted unit tests for the changed behavior. Full AWF/GitHub
   validation remains owned by AWF after agent completion.

## Expected Outcome

All affected runner-based `cat-file` checks execute with git object lookup
override variables removed, preventing inherited private object stores from
making missing commits appear valid.
