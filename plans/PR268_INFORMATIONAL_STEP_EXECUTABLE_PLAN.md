# PR 268 Informational Step Executable Plan

## Problem Statement and Scope

Protected workflow diffs allow added or edited informational/comment/notify steps, but the informational-step classifier currently accepts a marker-labeled step with neither `run` nor `uses`. GitHub Actions steps need executable semantics, so an unowned protected workflow diff can be allowed even though it would fail workflow parsing or execution.

Scope is limited to the protected workflow quality-gate helper and focused unit regression coverage.

## Requirements Checklist

- Add a regression test proving a marker-labeled protected workflow step with neither `run` nor `uses` is blocked.
- Require informational steps to include exactly one executable key: `run` or `uses`.
- Preserve existing allowed informational `run` and comment/notify `uses` behavior.
- Run the narrow unit coverage for the changed quality-gate tests.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_quality_gates.py` for adding a label-only informational step to a protected workflow.
2. Update `_is_informational_step()` in `src/awf/control/quality_gates.py` to reject steps unless exactly one of `run` or `uses` is present.
3. Run the new test first to confirm the old behavior fails, then run the focused quality-gate unit tests after implementation.
4. Create `plans/PR268_INFORMATIONAL_STEP_EXECUTABLE_VALIDATION.md` with requirement-by-requirement evidence.
5. Stage only changed files and commit with the requested thread-specific conventional commit message.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passes with the new regression and existing quality-gate coverage.
