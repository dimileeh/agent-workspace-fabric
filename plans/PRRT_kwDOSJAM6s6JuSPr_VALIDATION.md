# PRRT_kwDOSJAM6s6JuSPr Duplicate Lookup Validation

## Plan Check
- Added focused duplicate-PR regression coverage for a failed reconciliation lookup followed by a successful retry.
- Added focused coverage for the terminal case where duplicate reconciliation lookups are exhausted.
- Updated `src/awf/runtime/pr_creator.py` so duplicate lookup failures retry within the existing PR-create retry budget and report `duplicate_lookup_failed` when exhausted.

## Evidence
- Confirmed the new retry regression failed before the production change:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_creator.py::TestPushAndOpen::test_github_duplicate_pr_create_retries_failed_lookup -q`
- Passed focused runtime tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_creator.py::TestPushAndOpen::test_github_duplicate_pr_create_retries_failed_lookup tests/unit/runtime/test_pr_creator.py::TestPushAndOpen::test_github_duplicate_pr_create_reports_exhausted_lookup_failure tests/unit/runtime/test_pr_creator.py::TestPushAndOpen::test_github_duplicate_pr_create_error_reconciles_existing_pr -q`
- Passed targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_creator.py tests/unit/runtime/test_pr_creator.py`

## Broad Validation
Full AWF/GitHub validation is managed after agent completion; it was not run during this focused fix cycle.
