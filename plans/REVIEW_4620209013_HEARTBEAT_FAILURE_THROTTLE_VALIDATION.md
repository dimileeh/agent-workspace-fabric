# Review 4620209013 Heartbeat Failure Throttle Validation

Plan reference:
`plans/REVIEW_4620209013_HEARTBEAT_FAILURE_THROTTLE_PLAN.md`

## Requirement Status

- Add a focused regression proving repeated failed heartbeat writes are
  throttled inside the heartbeat write interval: Complete.
- Preserve the existing behavior that failures are logged and a later call
  after the write interval retries the heartbeat write: Complete.
- Keep changes minimal in `ControlWorker._record_heartbeat_safely()`: Complete.
- Run targeted tests for the changed behavior only: Complete.

## Advisory Readiness Observation

The comment's `/readyz` deployment timing point is a valid operator/runbook
consideration, but it does not identify a code defect in the current workspace
change. The hard worker heartbeat gate remains the intended smoke/readiness
contract for this PR.

## Evidence

Files changed:

- `src/awf/control/worker/manager.py`
- `tests/unit/control/test_worker_stop.py`
- `plans/REVIEW_4620209013_HEARTBEAT_FAILURE_THROTTLE_PLAN.md`
- `plans/REVIEW_4620209013_HEARTBEAT_FAILURE_THROTTLE_VALIDATION.md`

TDD failure observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py::test_record_heartbeat_safely_throttles_failed_writes -q`
  failed because two immediate handled heartbeat failures called
  `_record_heartbeat()` twice instead of throttling the second attempt.

Focused checks after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py::test_record_heartbeat_safely_throttles_failed_writes -q`:
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_stop.py -q -k "record_heartbeat_safely_throttles"`:
  passed, `2 passed, 9 deselected`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/manager.py tests/unit/control/test_worker_stop.py`:
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns the broad
post-agent validation suite and merge-gating provenance.
