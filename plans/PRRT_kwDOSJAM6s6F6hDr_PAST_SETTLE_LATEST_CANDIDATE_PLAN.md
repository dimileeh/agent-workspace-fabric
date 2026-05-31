# PRRT_kwDOSJAM6s6F6hDr Past-Settle Latest Candidate Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F6hDr` reports that past-settle remonitor
can warn that auto-merge timing has been re-entered while freezing only heads
found in persisted settle state plus an open merge-candidate head. Failed
workspaces can have their candidate closed before remonitor reopens it, so the
candidate head that the monitor will resume against is not included when there
is no currently open candidate.

Scope is limited to remonitor past-settle head selection, focused service
coverage, and the matching plan/validation records.

## Requirements Checklist

- Add a regression for past-settle remonitor with no open candidate where a
  latest closed candidate carries the live PR head.
- Preserve the existing stale `monitor_last_commit_sha` regression when no
  candidate head is available.
- Include the latest workspace merge-candidate head as a freeze target only
  when no open candidate head is available.
- Keep the warning/result/event behavior unchanged.
- Run focused local checks only; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add the closed-candidate remonitor regression and confirm it fails before
   implementation when practical.
2. Add a repository helper to read the latest merge candidate for a workspace
   regardless of status.
3. Use that helper in remonitor head selection as a fallback when no open
   candidate head exists.
4. Run targeted remonitor tests plus narrow lint/type checks for touched files.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_failed_workspace_past_settle_arms_latest_closed_candidate_head"`
  - Fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py -q -k "remonitor_no_reason_past_settle or remonitor_failed_workspace_past_settle"`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/db/repositories/quality_repo.py src/awf/service/controls.py tests/unit/service/test_controls_lifecycle_parts/test_controls_lifecycle_part_001.py`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev mypy src/awf/db/repositories/quality_repo.py src/awf/service/controls.py`
  - Passes after implementation.

Full AWF/GitHub validation, coverage gates, and CI-equivalent checks are not
run during this agent phase.
