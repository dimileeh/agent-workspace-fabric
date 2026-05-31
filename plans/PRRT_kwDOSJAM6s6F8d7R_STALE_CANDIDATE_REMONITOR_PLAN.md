# PRRT_kwDOSJAM6s6F8d7R Stale Candidate Remonitor Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F8d7R` reports that remonitor past-settle
selection can trust an open merge candidate head that lags a monitor repair
push. In that state `workspace.monitor_last_commit_sha` records the newer PR
head, but `candidate.head_sha` still points at the older candidate row. Passing
the stale candidate head as `current_head_sha` suppresses elapsed settle state
for the actual PR head, so remonitor may skip the warning and freeze.

Scope is limited to remonitor past-settle head selection and focused
regression coverage. Existing protections against freezing a genuinely newer
candidate head from stale workspace state must remain intact.

## Requirements Checklist

- Add a regression where an open candidate head is older than the workspace
  monitor head and only the workspace head has elapsed reviewer-settle state.
- Prefer the workspace monitor head in that lagging-candidate case so remonitor
  warns and re-arms freeze markers for the actual PR head.
- Preserve existing behavior where a newer/current candidate head does not
  inherit stale elapsed markers from `workspace.monitor_last_commit_sha`.
- Keep validation focused; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add a service-level remonitor regression in the controls lifecycle tests.
2. Confirm the regression fails against the current implementation.
3. Update remonitor current-head selection in `src/awf/service/controls.py` so
   workspace monitor head can override a stale open candidate only when the
   workspace row is fresher and persisted settle state matches that workspace
   head.
4. Run the focused controls regression set and a targeted ruff check for
   touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale"`
  - Fails before implementation and passes after.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_arms_current_candidate_head or remonitor_no_reason_stale_past_settle_does_not_arm_current_candidate_head or remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale"`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Passes after implementation.

Full AWF/GitHub validation, coverage gates, and CI-equivalent commands are not
run during this agent phase.
