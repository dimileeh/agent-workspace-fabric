# Address PRRT_kwDOSJAM6s6HAKXp Plan

## Problem Statement and Scope

The review thread reports that `ControlWorker.run_forever()` starts the background heartbeat task before the first `run_once()`, and both code paths attempt an initial heartbeat write. Because the repository performs a read-before-insert, concurrent cold-start writers for the same worker can race into duplicate inserts.

Scope is limited to the worker heartbeat startup sequencing and focused regression coverage.

## Requirements Checklist

- Verify the reported race against the current worker and repository code.
- Add focused regression coverage that fails before the sequencing fix.
- Ensure the background heartbeat loop does not perform an immediate startup write that races with `run_once()` in `run_forever()`.
- Preserve prompt stop behavior for `run_forever()` and `_heartbeat_loop()`.
- Run only targeted local validation; broad AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Inspect `src/awf/control/worker/manager.py` and the heartbeat repository path.
2. Add a unit test proving `_heartbeat_loop()` defers its first write.
3. Update `_heartbeat_loop()` to wait for one heartbeat interval, waking early on stop, before recording.
4. Run the focused worker heartbeat tests.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q`
  - Passes with the new regression test.
  - Confirms existing run-forever stop behavior and heartbeat write behavior still pass.

Full AWF/GitHub validation is intentionally not run inside this agent phase.
