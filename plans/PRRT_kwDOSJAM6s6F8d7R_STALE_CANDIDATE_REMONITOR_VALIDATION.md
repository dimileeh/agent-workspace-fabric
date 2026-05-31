# PRRT_kwDOSJAM6s6F8d7R Stale Candidate Remonitor Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F8d7R_STALE_CANDIDATE_REMONITOR_PLAN.md`

## Requirement Status

- Complete: Added a regression where an open merge candidate head lags
  `workspace.monitor_last_commit_sha` and only the workspace head has elapsed
  reviewer-settle state.
- Complete: Remonitor now prefers the fresher workspace monitor head in that
  lagging-candidate case, so the past-settle warning and freeze are re-armed
  for the actual PR head.
- Complete: Existing stale-workspace protections still pass for the case where
  a current candidate head must not inherit stale workspace settle markers.
- Complete: Focused local checks were used only. Full AWF/GitHub validation,
  coverage gates, and CI-equivalent commands remain owned by AWF after agent
  completion.

## Evidence

- Changed `src/awf/service/controls.py` to select the workspace monitor head
  when the open candidate row is older, the heads differ, and persisted settle
  state has elapsed for the workspace head.
- Added
  `test_remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale`
  in
  `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`.

## Commands Run

- Failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale"`
  - Failure: response warnings were `[]` instead of `REMONITOR_PAST_SETTLE`.
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale"`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle_arms_current_candidate_head or remonitor_no_reason_stale_past_settle_does_not_arm_current_candidate_head or remonitor_no_reason_past_settle_prefers_workspace_head_when_open_candidate_stale"`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head or remonitor_failed_workspace_past_settle_persists_operator_hint_and_warns or remonitor_failed_workspace_past_settle_uses_elapsed_marker_when_last_sha_stale"`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- Passed:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf/service/controls.py`

## Remaining Gaps

None for this thread. Broad validation and merge-gating provenance are
intentionally left to AWF/GitHub after this agent phase.
