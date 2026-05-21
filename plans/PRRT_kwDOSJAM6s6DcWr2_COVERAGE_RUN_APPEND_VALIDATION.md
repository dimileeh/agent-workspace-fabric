# PRRT_kwDOSJAM6s6DcWr2 Coverage Run Append Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DcWr2_COVERAGE_RUN_APPEND_PLAN.md`

## Requirement Status

- Block appended `coverage run ...` commands in unowned protected workflow
  validation-run edits: Complete.
- Block equivalent wrapped/module forms, including `uv run coverage run ...`
  and `python -m coverage run ...`: Complete.
- Preserve existing allowed validation broadening for non-executing coverage
  report/output subcommands such as `coverage html` and `coverage xml`:
  Complete.
- Keep unrelated protected workflow policies unchanged: Complete.
- Commit the local fix without pushing or changing branches: Complete.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DcWr2_COVERAGE_RUN_APPEND_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DcWr2_COVERAGE_RUN_APPEND_VALIDATION.md`

TDD failure observed before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_validation_run_preservation_allows_only_safe_validation_appends -q`
- Result: failed on unsafe `coverage run`, `uv run coverage run`,
  `python -m coverage run`, and `npm exec coverage run` append cases.

Verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_validation_run_preservation_allows_only_safe_validation_appends -q`
- Result: 21 passed.

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
- Result: 227 passed.

- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
- Result: passed.

## Gaps

No implementation gaps remain.
