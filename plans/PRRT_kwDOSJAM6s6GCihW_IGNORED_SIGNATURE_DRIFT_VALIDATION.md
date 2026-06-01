# PRRT_kwDOSJAM6s6GCihW Ignored Signature Drift Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6GCihW_IGNORED_SIGNATURE_DRIFT_PLAN.md`

## Requirement Status

- Detect content-signature drift for ignored snapshot paths captured at setup:
  Complete. `src/awf/control/executor/execution_validation.py` now stores setup
  ignored snapshot signatures and compares them on later validation attempts.
- Preserve existing added/removed ignored-root and ignored-path drift behavior:
  Complete. The existing root/path drift comparison remains unchanged, with
  signature drift appended to the same drift result.
- Add a regression test where ignored roots and paths stay constant but a
  baseline ignored file signature changes: Complete.
  `test_execution_validation_rejects_ignored_signature_drift_after_fix_pass`
  covers same-path signature drift after a validation fix pass.
- Run only targeted local checks: Complete. Full AWF/GitHub validation is
  managed by AWF after agent completion and was not run locally.

## Evidence

- Red phase:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "signature_drift"`
  failed before the implementation because validation ran a second profile pass.
- Green focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "signature_drift"`
  passed.
- Focused ignored-drift coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py -q -k "ignored"`
  passed with 4 tests.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
  passed.
- Targeted format check:
  `uv run --python 3.12 --extra dev ruff format --check src/awf/control/executor/execution_validation.py tests/unit/control/test_executor_coverage_edges_parts/test_executor_coverage_edges_part_003.py`
  passed.
