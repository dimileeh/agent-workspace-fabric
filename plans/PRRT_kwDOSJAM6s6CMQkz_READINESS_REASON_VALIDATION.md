# PRRT_kwDOSJAM6s6CMQkz Readiness Reason Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CMQkz_READINESS_REASON_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving pretty output omits `reason:` for
  `ok` checks.
  - Evidence: `tests/unit/service/test_readiness.py` adds
    `test_core_readiness_pretty_omits_ok_reason_and_keeps_failure_reason`.
- Complete: Preserve `reason:` output for non-`ok` checks.
  - Evidence: the same regression test asserts the failing SLO check still
    renders `reason: PRD_SLO_THRESHOLDS_FAILED`.
- Complete: Preserve evidence rendering for checks whose reason line is
  omitted.
  - Evidence: the same regression test asserts `evidence status: ok` remains in
    pretty output for the `ok` service status check.
- Complete: Keep JSON serialization and readiness collection behavior
  unchanged.
  - Evidence: implementation only changes `render_core_readiness_pretty`; the
    structured `to_dict` path was not changed.
- Complete: Run focused validation for the changed behavior.
  - Evidence: commands below.

## Test Evidence

- Red test before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py::test_core_readiness_pretty_omits_ok_reason_and_keeps_failure_reason -q`
  - Result: failed because `reason: SERVICE_STATUS_OK` was present.
- Green focused test after implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py::test_core_readiness_pretty_omits_ok_reason_and_keeps_failure_reason -q`
  - Result: passed.
- Readiness unit module:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py -q`
  - Result: passed, 23 tests.
- CLI pretty output surface:
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_service_cli.py::test_service_readiness_pretty_labels_release_gate_and_summarizes_checks -q`
  - Result: passed.
- Lint:
  - `uv run --python 3.12 --extra dev ruff check src/awf/service/readiness.py tests/unit/service/test_readiness.py`
  - Result: passed.

## Remaining Gaps

None.
