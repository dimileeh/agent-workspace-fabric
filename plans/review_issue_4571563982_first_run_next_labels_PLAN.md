# Review Issue 4571563982 First-Run Next Labels Plan

## Problem Statement and Scope

Address PR review comment `issue:4571563982`: pretty first-run output can render two unlabeled `Next:` sections when both issue remediation `next_steps` and top-level payload `next_steps` are populated. Keep JSON behavior unchanged and make the pretty output unambiguous for direct `FirstRunPayload` callers.

## Requirements Checklist

- Add a focused regression test for pretty output with both remediation-level and payload-level next steps.
- Distinguish issue remediation next steps from command-level next steps in pretty output.
- Preserve existing JSON payload shape and existing top-level `Next:` rendering.
- Keep changes scoped to renderer behavior, tests, and required plan/validation docs.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_host_setup_rendering.py` for the dual next-step scenario.
2. Update `src/awf/host_setup/rendering.py` to label issue remediation next steps distinctly.
3. Run the targeted test module or specific test selection needed to prove the fix.
4. Record validation evidence in `plans/review_issue_4571563982_first_run_next_labels_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
- Pass criteria: the focused rendering tests pass, with no broad AWF/GitHub validation run locally because AWF owns broad validation after agent completion.
