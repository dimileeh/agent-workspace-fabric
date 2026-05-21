# Review PRRT_kwDOSJAM6s6DxZ5k Replacement Attempt Blocked Validation

Plan reference:
`REVIEW_PRRT_kwDOSJAM6s6DxZ5k_REPLACEMENT_ATTEMPT_BLOCKED_PLAN.md`

## Requirement status

- Regression test for existing replacement without replacement attempt:
  Complete. Updated
  `test_preserved_active_replacement_missing_existing_attempt_records_blocked`.
- Record exactly one current `workspace.active_execution_salvage_blocked`
  event with `blocked_reason="replacement_attempt_missing"`:
  Complete. The regression calls the replacement recovery path twice and
  asserts one blocked event.
- Keep replacement-created salvage events absent until a replacement attempt
  exists:
  Complete. The regression asserts no replacement-created event is recorded.
- Preserve the warning signal with accurate salvage reason context:
  Complete. The warning remains and now carries
  `ACTIVE_EXECUTION_SALVAGE_BLOCKED`.
- Avoid unrelated stale-active recovery changes:
  Complete. The implementation only extends the existing blocked-event writer
  with optional extra payload fields and uses it from the missing-attempt branch.

## Evidence

- Changed files:
  - `src/awf/control/worker.py`
  - `tests/unit/control/test_worker.py`
  - `plans/REVIEW_PRRT_kwDOSJAM6s6DxZ5k_REPLACEMENT_ATTEMPT_BLOCKED_PLAN.md`
  - `plans/REVIEW_PRRT_kwDOSJAM6s6DxZ5k_REPLACEMENT_ATTEMPT_BLOCKED_VALIDATION.md`
- Failing-before evidence:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k preserved_active_replacement_missing_existing_attempt`
    failed with `assert 0 == 1` for missing blocked events before the
    implementation.
- Passing-after evidence:
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k preserved_active_replacement_missing_existing_attempt`
  - `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_replacement"`
  - `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
  - `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`

## Gaps

None.
