# PRRT_kwDOSJAM6s6F786O Grok Runtime Reason Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6F786O_GROK_RUNTIME_REASON_PLAN.md`

## Requirement Status

- Complete: Added a focused regression in
  `tests/unit/service/test_doctor_reasons.py` proving
  `GROK_RUNTIME_CLI_NOT_FOUND` has doctor guidance.
- Complete: Added `_ReasonText` for `GROK_RUNTIME_CLI_NOT_FOUND` in
  `src/awf/service/doctor/reasons.py`.
- Complete: Updated `docs/REASON_CATALOG.md` with the generated reason catalog
  entry.
- Complete: Ran focused checks only; full AWF/GitHub validation remains owned by
  AWF after agent completion.

## Evidence

- Red test before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py -q`
  failed with `KeyError: 'GROK_RUNTIME_CLI_NOT_FOUND'`.
- Passing reason catalog tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py -q`
  passed with `5 passed`.
- Passing preflight emitter regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_selected_grok_preflight_blocks_missing_runtime_cli_and_redacts_key -q`
  passed with `1 passed`.
- Passing focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/service/doctor/reasons.py tests/unit/service/test_doctor_reasons.py`
  passed.

## Remaining Gaps

None for this review thread. Full repository validation, coverage gates, and
CI-equivalent checks were intentionally not run in the agent phase per AWF
workspace contract.
