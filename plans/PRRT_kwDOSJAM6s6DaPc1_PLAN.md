# PRRT_kwDOSJAM6s6DaPc1 Plan

## Problem Statement and Scope

The missing-HEAD recovery verification in `src/awf/control/executor.py` classifies
recovered committed paths before allowing the recovered `HEAD` to continue. The
review thread reports that this path uses `_committed_paths_since`, which is
based on `git diff --name-only` and can miss the protected source side of a
rename. The scope is limited to recovered post-agent commit verification and
its regression coverage.

## Requirements Checklist

- Add a failing regression test showing that recovered missing-HEAD verification
  blocks a rename from an unowned protected quality-gate path to an unprotected
  path.
- Reuse the existing rename-aware committed-path parser used by the pre-push
  guardrail rather than adding an ad hoc parser.
- Preserve existing plan-only path and protected quality-gate behavior for
  recovered commits.
- Keep changes scoped to executor behavior, focused tests, and this plan and
  validation record.

## Implementation Steps

1. Inspect the existing committed-path and protected-file diff helpers.
2. Add a regression test near the existing recovered post-agent commit tests.
3. Confirm the new test fails against the current implementation.
4. Update recovered post-agent commit verification to derive `changed_paths`
   from the rename-aware committed path helper.
5. Run focused tests, then broader unit checks if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/control/test_executor_coverage_edges.py`
  passes.
