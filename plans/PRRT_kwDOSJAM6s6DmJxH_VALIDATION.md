# PRRT_kwDOSJAM6s6DmJxH Validation

Plan reference: `PRRT_kwDOSJAM6s6DmJxH_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving active worker-restart
  `OperationType.rebase` rows with `recovery_mode="rebase_only"` are treated as
  active preserved salvage.
- Complete: Existing validate-only and validate-row `rebase_only` behavior is
  preserved by the `validate` branch of the SQL predicate.
- Complete: Source, status, workspace, reason-code, and preservation-event
  filters remain in the lookup.
- Complete: Targeted validation was run for the changed worker behavior.

## Evidence

Changed files:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`

TDD evidence:

- Failing before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k preserved_active_validation_recovery_lookup_includes_active_rebase_operation -q`
- Passing after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_validation_recovery_lookup or preserved_active_existing_rebase_recovery_redispatches or preserved_active_existing_validation_recovery_redispatch_logs" -q`

Additional validation:

- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/worker.py`
