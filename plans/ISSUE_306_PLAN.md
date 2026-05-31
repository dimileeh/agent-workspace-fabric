# Issue 306 Plan

## Problem Statement And Scope

Adopted PR monitors can receive operator-declared `owned_paths` such as
`.github/workflows/publish.yml`. Those paths may also match generic protected
quality-gate patterns. AWF must treat explicitly owned protected paths as
editable by the repair agent while still preserving the separate GitHub push
permission boundary for workflow files that require a token with `workflow`
scope.

This plan implements the saved workspace contract in
`docs/awf-plans/ws_e4102ead0ebd495fba3fd2f4.md` for GitHub issue #306.

## Requirements Checklist

- Propagate `owned_paths` into protected-file diff classification and PR monitor
  protected-scope policy checks.
- Render repair prompts so owned protected paths are explicitly editable and
  only unowned protected paths require generic protected-file approval.
- Detect GitHub push failures caused by missing `workflow` scope separately
  from generic push failures.
- Route missing-workflow-scope push failures to merge-blocking `NEEDS_HUMAN`
  behavior with a clear permission reason and reason code.
- Add focused regression coverage for owned workflow editability and
  owned-but-unpushable workflow permission blockers.
- Keep changes generic to GitHub/AWF behavior and avoid broad validation inside
  the AWF agent phase.

## Implementation Steps

1. Add failing regression tests for owned protected paths being excluded from
   diff-classified protected file loading.
2. Add failing prompt tests proving owned protected paths are rendered as
   editable, not generic protected-file approval blockers.
3. Add failing push/fix-cycle tests for GitHub missing-`workflow`-scope stderr
   mapping to a specific permission blocker and `needs_human` state.
4. Implement ownership-aware protected diff classification and thread it through
   control-plane and PR monitor callers.
5. Implement owned-path-aware repair prompt policy text.
6. Implement missing-workflow-scope push detection and route that reason through
   the existing two-kind verdict model as `NEEDS_HUMAN`.
7. Write `plans/ISSUE_306_VALIDATION.md` with requirement status and focused
   verification evidence.

## Verification Commands And Pass Criteria

Focused checks only:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_protected_file_diffs.py tests/unit/control/test_quality_gates_parts/test_quality_gates_part_001.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_001.py -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_004.py -q
uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py -q
uv run --python 3.12 --extra dev ruff check <touched files>
uv run --python 3.12 --extra dev mypy <touched src files>
```

Pass criteria: the targeted regressions pass, maintainability guard remains
green for touched PR monitor files, focused lint/type checks pass for touched
files, and the validation note states that broad AWF/GitHub validation is
managed after agent completion.
