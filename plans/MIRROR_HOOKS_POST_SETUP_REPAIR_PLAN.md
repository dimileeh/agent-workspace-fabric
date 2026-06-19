# Mirror Hooks Post-Setup Repair Plan

## Problem Statement And Scope

Inline review thread `PRRT_kwDOSJAM6s6K7kwT` reports that successful profile setup can poison the shared mirror `core.hooksPath`, while recovery and approve-and-keep resumes can skip the later agent-launch repair. Scope is limited to the executor mirror-hook repair path in `execution_flow.py` and its focused unit regressions.

## Requirements Checklist

- Repair the shared mirror hooks path after successful setup/pre-agent profile phases.
- Fail closed if that post-success repair fails before recovery or skip-agent paths can continue to validation, skip-push, or PR push.
- Preserve existing pre-setup, setup-failure, before-agent-launch, and post-agent repair behavior.
- Add/update focused tests for the new guard and changed call ordering.

## Implementation Steps

1. Insert `_repair_mirror_hooks_path_or_mark_failed(failure_stage="after successful profile setup")` after `setup_result.all_passed` is confirmed and before any success-path branch can continue.
2. Add a regression that runs a recovery/skip-agent success path and verifies post-success repair failure marks the workspace failed.
3. Update existing mirror-hook tests whose repair call counts shift because the new success repair runs before the agent-launch/post-agent guards.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
- Pass criteria: focused mirror-hook executor tests pass. Full AWF/GitHub validation remains owned by AWF after this agent phase.
