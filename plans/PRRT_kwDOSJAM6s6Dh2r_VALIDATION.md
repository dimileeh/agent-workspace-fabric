# PRRT_kwDOSJAM6s6Dh2r Validation

Plan reference: `PRRT_kwDOSJAM6s6Dh2r_PLAN.md`

## Requirement Status

- Accept PR URLs whose `/pull/<number>` segment is followed by `?query`:
  Complete.
- Accept PR URLs whose `/pull/<number>` segment is followed by `#fragment`:
  Complete.
- Preserve existing support for canonical, trailing slash, and subpath PR URLs:
  Complete.
- Preserve rejection of non-PR URLs and non-numeric PR numbers: Complete.
- Keep the duplicate worker and executor extraction helpers consistent:
  Complete.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `src/awf/control/executor.py`
- `tests/unit/control/test_worker.py`
- `tests/unit/control/test_executor.py`

Verification:

- Initial regression check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::test_extract_pr_number_accepts_query_and_fragment_boundaries -q`
  failed on direct query and fragment PR URLs.
- Targeted helper tests after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py::test_extract_pr_number_accepts_query_and_fragment_boundaries tests/unit/control/test_executor.py::TestPrNumberExtraction -q`
  passed with 14 tests.
- Salvage behavior regression surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pr_handoff_derives_missing_pr_number_before_attach or preserved_active_pr_handoff_without_pr_number_does_not_attach_monitor or preserved_active_recovery_defers_pr_number_write_to_locked_attach" -q`
  passed with 3 tests.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py src/awf/control/executor.py tests/unit/control/test_worker.py tests/unit/control/test_executor.py`
  passed.

## Gaps

None.
