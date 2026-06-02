# Orphan Stop Timeout Plan

## Problem Statement And Scope

PR review comment `issue:4585090228` reports that
`Provisioner._launch_lost_to_terminal_cleanup()` calls
`stop_project_containers()` without a timeout. If Docker hangs during orphan
container cleanup, the provisioner task can hang indefinitely.

Scope is limited to the provisioner orphan-stop path and its focused regression
coverage.

## Requirements Checklist

- Add a bounded per-operation timeout around orphan container stop during
  launch-lost-to-terminal cleanup.
- Preserve the existing failure behavior: record
  `orphan_containers_stopped=false`, include `orphan_stop_error`, and revoke the
  prior terminal runtime release when orphan stop fails.
- Add regression coverage proving a hung orphan stop is converted into the same
  recorded failure path instead of hanging the provisioner.
- Run only focused validation for the changed provisioner behavior; broad
  AWF/GitHub validation is managed after agent completion.

## Implementation Steps

1. Add a failing unit test beside the existing orphan-stop failure tests.
2. Introduce a timeout constant and wrap `stop_project_containers()` with
   `asyncio.wait_for()`.
3. Catch `asyncio.TimeoutError` separately and persist a clear timeout error in
   the existing revoke/stale-skip payload flow.
4. Re-run the focused provisioner test(s) touched by this change.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_002.py -q`

Pass criteria: the focused provisioner test module passes locally. Full
repository validation remains owned by AWF/GitHub after this agent phase.
