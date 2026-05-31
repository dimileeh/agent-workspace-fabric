# PRRT_kwDOSJAM6s6F5wuq Validation

Plan reference: `PRRT_kwDOSJAM6s6F5wuq_PLAN.md`

## Requirement Status

- Complete: Added a regression test where `monitor_last_commit_sha` differs
  from the elapsed non-check reviewer-settle marker stored in monitor state.
- Complete: Kept existing exact-SHA behavior and warning/freeze response intact.
- Complete: Stale SHA remonitor now detects elapsed settle markers from
  persisted monitor state, persists the operator hint, emits the past-settle
  warning, and re-arms settle for the detected marker head SHA.
- Complete: Used only focused local checks; full AWF/GitHub validation is
  managed by AWF after agent completion.

## Evidence

- Changed `src/awf/runtime/operator_hints.py` to expose
  `remonitor_elapsed_settle_head_shas`, which prefers the workspace SHA when it
  has elapsed settle state and otherwise parses persisted settle marker keys.
- Changed `src/awf/service/controls.py` so remonitor freezes against detected
  elapsed settle head SHAs instead of only `workspace.monitor_last_commit_sha`.
- Added
  `test_remonitor_failed_workspace_past_settle_uses_elapsed_marker_when_last_sha_stale`
  in
  `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`.

## Commands Run

- Failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "uses_elapsed_marker_when_last_sha_stale"`
- Passed after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "uses_elapsed_marker_when_last_sha_stale"`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle"`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/operator_hints.py src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q -k "test_remonitor_past_settle_persists_operator_hint_and_warns"`
- Passed:
  `git diff --check`

## Remaining Gaps

None for the planned scope. Full repository validation, coverage gates, and CI
provenance are intentionally left to AWF/GitHub after this agent phase.
