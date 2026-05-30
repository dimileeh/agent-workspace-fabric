# Case-Variant First-Run Redaction Validation

Plan reference: `review_PRRT_kwDOSJAM6s6Ft3w_case_variant_first_run_redaction_PLAN.md`

## Requirement Status

- Add a regression test showing first-run JSON output redacts case-variant token prefixes: Complete.
- Add a regression assertion showing pretty output redacts the same case-variant tokens: Complete.
- Preserve provider-ref redaction and mapping-key collision behavior: Complete.
- Keep validation focused to the changed unit test surface; broad AWF/GitHub validation is handled after agent completion: Complete.

## Evidence

Files changed:

- `src/awf/host_setup/rendering.py`
- `tests/unit/service/test_host_setup_rendering.py`
- `plans/review_PRRT_kwDOSJAM6s6Ft3w_case_variant_first_run_redaction_PLAN.md`
- `plans/review_PRRT_kwDOSJAM6s6Ft3w_case_variant_first_run_redaction_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_rendering_redacts_case_variant_token_prefixes -q`
  - First run failed before implementation, confirming the regression.
  - Second run passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
  - Passed: 38 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/rendering.py tests/unit/service/test_host_setup_rendering.py`
  - Passed.

Full AWF/GitHub validation, coverage gates, and merge-gating checks are intentionally not run in the agent phase; AWF owns those after agent completion.
