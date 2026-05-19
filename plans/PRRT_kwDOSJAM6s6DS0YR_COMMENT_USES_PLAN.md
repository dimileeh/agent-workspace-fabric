# PRRT_kwDOSJAM6s6DS0YR Comment Uses Plan

## Problem Statement And Scope

The protected workflow classifier allows `continue-on-error: true` only for
comment/notify steps, but `_is_comment_or_notify_step` currently inspects only
`id` and `name`. Existing workflow steps that are identified only by a comment
action in `uses` can be incorrectly blocked when `continue-on-error` is added.

Scope is limited to review thread `PRRT_kwDOSJAM6s6DS0YR` in
`src/awf/control/quality_gates.py` and a regression test for the classifier.

## Requirements Checklist

- Add a regression test for an existing uses-only comment action that gains
  `continue-on-error: true`.
- Keep added workflow steps with arbitrary `uses` actions blocked by existing
  protections.
- Update comment/notify step detection to inspect `uses` in addition to
  `id` and `name`.
- Run the narrow unit test that proves the regression fix.

## Implementation Steps

1. Add a failing unit test in `tests/unit/control/test_quality_gates.py`.
2. Run the narrow test to confirm the current behavior fails.
3. Update `_is_comment_or_notify_step` to include the normalized action name
   from `uses` when present.
4. Re-run the narrow unit test and relevant quality checks.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
