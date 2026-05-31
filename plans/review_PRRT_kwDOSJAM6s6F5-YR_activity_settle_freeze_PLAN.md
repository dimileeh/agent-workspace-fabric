# Review PRRT_kwDOSJAM6s6F5-YR Activity Settle Freeze Plan

## Problem Statement And Scope

An operator remonitor issued after a non-check reviewer quiet window elapsed should freeze auto-merge long enough for the monitor to re-evaluate the operator hint and reviewer activity. Activity-anchored settle decisions use state keys that include an activity signature, but the freeze helper currently re-arms only the base head-SHA started key. When the next PR snapshot still has the old `quiet_period_anchor_at`, the activity path can immediately mark the same quiet window elapsed again.

Scope is limited to reviewer-settle state helpers, the operator-hint freeze helper, focused regression tests, and this plan/validation record.

## Requirements Checklist

- Preserve existing non-activity settle freeze behavior for base head-SHA keys.
- Re-arm elapsed activity-signature settle state so the activity-aware decision honors a fresh freeze period after remonitor.
- Remove prior elapsed markers for the affected head/signature so old elapsed state cannot bypass the freeze.
- Keep new state scoped to the relevant PR number and head SHA.
- Record focused validation evidence only; AWF/GitHub own broad post-agent validation.

## Implementation Steps

1. Add a focused regression covering a remonitor freeze after an activity-anchored elapsed marker.
2. Confirm the new regression fails against the current implementation.
3. Update the freeze helper to preserve activity signatures when re-arming elapsed activity settle state.
4. Update the activity-aware settle decision to consume a persisted freeze start marker for its current signature before declaring elapsed.
5. Re-run the focused regression/test module and a narrow lint check for touched files.
6. Create the validation document with requirement-by-requirement status and focused command evidence.

## Verification Commands And Pass Criteria

- Red test: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k remonitor`
  - Pass criterion before implementation: the new regression fails because the activity-anchored quiet window immediately returns `elapsed`.
- Green tests: `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py -q -k "activity or remonitor"`
  - Pass criterion after implementation: activity-settle and remonitor-focused tests pass.
- Focused lint: `uv run --python 3.12 --extra dev ruff check src/awf/runtime/operator_hints.py src/awf/runtime/pr_monitor_runner/helpers.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py`
  - Pass criterion: no lint findings for changed Python files.

Full AWF/GitHub validation is intentionally not run during this agent phase.
