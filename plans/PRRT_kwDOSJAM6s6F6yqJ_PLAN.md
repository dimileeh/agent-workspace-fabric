# PRRT_kwDOSJAM6s6F6yqJ Plan

## Problem Statement And Scope

The non-check reviewer settle decision currently treats any head-scoped
`started_key` as a reason to keep waiting when all configured reviewers are now
visible. That is correct for an operator remonitor freeze, but it is too broad
for an ordinary wait that began while a reviewer check was missing.

Scope is limited to the non-check reviewer settle helper behavior and focused
unit coverage for the inline review thread.

## Requirements Checklist

- Add a regression test showing that an ordinary missing-reviewer wait skips
  the remaining settle window once the configured reviewer becomes visible.
- Preserve the existing remonitor freeze behavior that waits before visible
  reviewer skip.
- Keep state markers scoped and compatible with existing persisted settle
  conversion behavior.
- Run only targeted checks for the changed behavior; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Add the failing regression to
   `tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`.
2. Confirm the regression fails against the current implementation when
   practical.
3. Add an explicit head-scoped remonitor freeze marker and use it to decide
   whether a visible-reviewer state should keep waiting.
4. Update remonitor freeze setup and state merge behavior to preserve the new
   marker where needed.
5. Re-run targeted unit tests for non-check reviewer settle behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q`
  passes.
- If the initial regression-only run is practical, it fails before the
  implementation and passes after the implementation.
