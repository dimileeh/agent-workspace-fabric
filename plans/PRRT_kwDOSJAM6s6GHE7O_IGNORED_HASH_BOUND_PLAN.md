# PRRT_kwDOSJAM6s6GHE7O Ignored Hash Bound Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6GHE7O` reports that validation worktree
pre-checks hash every file under preserved ignored roots such as `.venv/`,
making validation startup scale with dependency-cache contents. The fix should
bound ignored-file content hashing while preserving cleanup safety for small
ignored snapshots and path-level drift detection for large ignored roots.

## Requirements Checklist

- Bound total ignored regular-file content bytes read during signature capture.
- Preserve existing ignored path snapshots so AWF can still detect added and
  deleted ignored entries under setup-owned ignored roots.
- Preserve content-hash modification detection for small ignored snapshots.
- Fall back to metadata signatures after the content-hash budget is exhausted.
- Add focused regression coverage for the bounded-signature behavior.
- Avoid broad AWF/GitHub-owned validation; record only targeted local checks.

## Implementation Steps

1. Add a failing unit test showing ignored snapshot signature capture stops
   reading file contents after a budget and uses metadata signatures beyond it.
2. Update `src/awf/runtime/validation_worktree.py` to apply a total content
   hash byte budget while building ignored path signatures.
3. Keep existing content-hash behavior for small files within the budget.
4. Run targeted tests for `tests/unit/runtime/test_validation_worktree.py`.
5. Create the matching validation document with evidence and any gaps.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation_worktree.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase because
  AWF owns broad validation, provenance, logs, and merge gating after completion.

## Assumptions/Changes

- The full `tests/unit/runtime/test_validation_worktree.py` command was run and
  exposed four pre-existing cleanup-behavior failures outside this review
  thread's hash-bound scope. Validation therefore records the full-file failure
  as unrelated evidence and uses focused signature/worktree-safety tests as the
  pass criteria for this thread-specific fix.
