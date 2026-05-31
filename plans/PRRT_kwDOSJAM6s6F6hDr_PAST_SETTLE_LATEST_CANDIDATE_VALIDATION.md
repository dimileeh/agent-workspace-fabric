# PRRT_kwDOSJAM6s6F6hDr Past-Settle Latest Candidate Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6F6hDr_PAST_SETTLE_LATEST_CANDIDATE_PLAN.md`

## Requirement Status

- Complete: Added a regression for past-settle remonitor with no open
  candidate where the latest closed candidate carries the live PR head.
- Complete: Preserved the existing stale `monitor_last_commit_sha` regression
  when no candidate head is available.
- Complete: Remonitor now uses the latest workspace merge-candidate head as a
  fallback freeze target only when no open candidate head is available.
- Complete: Warning/result/event behavior is unchanged; focused tests assert
  the existing warning payloads.
- Complete: Ran focused local checks only; full AWF/GitHub validation remains
  managed after agent completion.

## Evidence

Files changed:

- `src/awf/db/repositories/quality_repo.py`
- `src/awf/service/controls.py`
- `tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
- `plans/PRRT_kwDOSJAM6s6F6hDr_PAST_SETTLE_LATEST_CANDIDATE_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6F6hDr_PAST_SETTLE_LATEST_CANDIDATE_VALIDATION.md`

Focused TDD evidence:

- Red before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head"`
  - Failed because the live-head settle-started key was absent.

Focused checks after implementation:

- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head"`
  - Result: `1 passed, 36 deselected`.
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle or remonitor_failed_workspace_past_settle"`
  - Result: `5 passed, 32 deselected`.
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/quality_repo.py src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Result: `All checks passed!`.
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/quality_repo.py src/awf/service/controls.py`
  - Result: `Success: no issues found in 2 source files`.
- Passed:
  `git diff --check`
  - Result: no whitespace errors.

## Remaining Gaps

None for this thread. Broad validation, coverage gates, and CI-equivalent
checks are intentionally left to AWF/GitHub after this agent phase.
