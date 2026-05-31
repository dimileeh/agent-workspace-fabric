# Operator Hint Key Helpers Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F5x8O` reports that `src/awf/runtime/operator_hints.py`
duplicates grace and reviewer-settle state key helper functions that are canonical in
`awf.runtime.pr_monitor_runner.helpers`. The duplicate definitions can drift from the
runtime readers and leave stale elapsed state in persisted monitor metadata.

Scope is limited to making `operator_hints` reuse the canonical helper functions and
adding a targeted regression test for that import contract.

## Requirements Checklist

- [ ] `operator_hints` must not define duplicate initial-review-grace key helpers.
- [ ] `operator_hints` must not define duplicate non-check-reviewer-settle key helpers.
- [ ] A focused regression test must prove the helpers used by `operator_hints` are the
      canonical helper objects from `pr_monitor_runner.helpers`.
- [ ] Verification must use targeted tests/checks only; full AWF/GitHub validation is
      managed after agent completion.

## Implementation Steps

1. Add a failing regression test in `tests/unit/runtime/test_pr_monitor_operator_hints.py`
   that compares the four helper object identities between `operator_hints` and
   `pr_monitor_runner.helpers`.
2. Run that targeted test and confirm it fails with the current duplicated functions.
3. Replace local duplicate helper definitions in `src/awf/runtime/operator_hints.py` with
   imports from `awf.runtime.pr_monitor_runner.helpers`.
4. Re-run the focused test file or specific test to confirm the regression is fixed.

## Assumptions/Changes

- Importing `pr_monitor_runner.helpers` from `operator_hints` exposed an existing
  package-level eager import cycle through `pr_monitor_runner.__init__`. Preserve the
  public `PullRequestMonitorRunner` export with a lazy module `__getattr__` so helper
  imports can stay canonical without initializing the full runner.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py -q`
  passes.
- Full AWF/GitHub validation is intentionally not run in the agent phase.
