# PR 353 Reason Catalog CI Validation

Plan reference: `plans/PR353_REASON_CATALOG_CI_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Confirm the reported focused pytest failure locally. | Complete | Initial run of `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q` failed with missing `MERGE_METHOD_MISMATCH`. |
| Add `MERGE_METHOD_MISMATCH` to canonical doctor reason text. | Complete | `src/awf/service/doctor/reasons.py` now includes operator-facing text, likely cause, fix, related command, and docs link for `MERGE_METHOD_MISMATCH`. |
| Regenerate `docs/REASON_CATALOG.md`. | Complete | Ran `uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py`; `docs/REASON_CATALOG.md` now contains the generated `MERGE_METHOD_MISMATCH` entry. |
| Validate with focused checks only. | Complete | Focused checks passed: catalog coverage, catalog sync, and targeted Ruff on `src/awf/service/doctor/reasons.py`. Full AWF/GitHub validation was not run locally because AWF owns broad validation after agent completion. |
| Commit the scoped fix locally without pushing. | Complete | Included in the local commit for this scoped CI fix; no push was performed. |

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q`
  - First run: failed with missing `MERGE_METHOD_MISMATCH`.
  - Final run: passed.
- `uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py`
  - Regenerated `docs/REASON_CATALOG.md`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source -q`
  - Passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/doctor/reasons.py`
  - Passed.

## Gaps

None. Broad CI-equivalent validation remains delegated to AWF/GitHub per the
workspace contract.
