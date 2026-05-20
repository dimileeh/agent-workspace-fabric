# PRRT_kwDOSJAM6s6DaOTt Preserved Validation Recovery Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DaOTt_PLAN.md`

## Requirement Status

- Complete: Reproduced the stranded `validating`/`pushing` clean committed
  salvage path with focused regression coverage.
  - Evidence:
    `test_preserved_active_clean_committed_non_running_work_rewinds_for_validation_salvage`.
- Complete: Preserved the existing `running` salvage path.
  - Evidence:
    `test_preserved_active_clean_committed_work_dispatches_validation_salvage_once`.
- Complete: Moved non-running active execution salvage into an
  executor-claimable recovery status before dispatch.
  - Evidence: `src/awf/control/worker.py` transitions non-`running`
    validation salvage to `running` with
    `ACTIVE_EXECUTION_SALVAGE_VALIDATION_REQUESTED`.
- Complete: Recorded explicit state-change evidence and kept validation salvage
  idempotent.
  - Evidence: worker regression asserts the `workspace.state_changed` payload,
    single salvage event, and single validate operation. The queue-full
    regression proves later scans redispatch the pending operation without
    creating duplicate preservation or salvage records.
- Complete: Avoided stale-active cleanup for recoverable committed work.
  - Evidence: worker regression asserts no stale-active detection events and no
    cleaner calls.

## TDD Evidence

The new tests were added before implementation and failed as expected:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py tests/unit/control/test_worker.py -q -k "test_allowed or non_running_work_rewinds"
# 4 failed, 28 passed, 231 deselected

uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "rewound_validation_salvage_waits_without_duplicate"
# 1 failed, 197 deselected
```

## Commands Run

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "rewound_validation_salvage_waits_without_duplicate"
# 1 passed, 197 deselected in 2.43s

uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py tests/unit/control/test_worker.py -q -k "test_allowed or non_running_work_rewinds or rewound_validation_salvage_waits_without_duplicate"
# 33 passed, 231 deselected in 4.69s

uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "preserved_active_clean_committed or rewound_validation_salvage_waits_without_duplicate"
# 4 passed, 194 deselected in 6.94s

uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py tests/unit/control/test_worker.py -q
# 264 passed in 143.38s

uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/control/state_machine.py tests/unit/control/test_worker.py tests/unit/control/test_state_machine.py
# All checks passed

uv run --python 3.12 --extra dev ruff format --check src/awf/control/worker.py src/awf/control/state_machine.py tests/unit/control/test_worker.py tests/unit/control/test_state_machine.py
# 4 files already formatted

uv run --python 3.12 --extra dev mypy src/awf
# Success: no issues found in 157 source files
```

## Files Changed

- `src/awf/control/state_machine.py`
- `src/awf/control/worker.py`
- `tests/unit/control/test_state_machine.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6DaOTt_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DaOTt_VALIDATION.md`

No known gaps remain.
