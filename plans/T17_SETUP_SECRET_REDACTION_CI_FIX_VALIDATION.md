# T17 Setup Secret Redaction CI Fix Validation

Plan reference: `plans/T17_SETUP_SECRET_REDACTION_CI_FIX_PLAN.md`

## Requirement Status

- Complete: Reproduced the failing CI test locally before changing code.
  Evidence: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_follow_flushes_multiline_secret_prefix_at_eof -q` failed with `stdout <redacted>` before the fix.
- Complete: Preserve exact redaction of full multiline Compose secret values.
  Evidence: `tests/unit/service/test_logs_parts/test_logs_part_002.py` still passes, including the existing captured and followed multiline-redaction cases.
- Complete: Do not treat the first physical line of an unclosed quoted multiline
  Compose env assignment as a standalone exact secret.
  Evidence: Added `test_service_log_secret_values_excludes_multiline_first_line_fragment`; the CI-failing EOF test now passes.
- Complete: Keep validation focused.
  Evidence: Ran only targeted service-log tests plus focused lint/type checks for changed files. Full AWF/GitHub validation remains managed by AWF after agent completion.
- Complete: Commit the local repair.
  Evidence: This validation artifact is included in the local repair commit;
  AWF handles push and GitHub revalidation after agent completion.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_log_secret_values_excludes_multiline_first_line_fragment -q`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_follow_flushes_multiline_secret_prefix_at_eof -q`
  - Result: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q`
  - Result: 39 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/logs.py tests/unit/service/test_logs_parts/test_logs_part_002.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/logs.py`
  - Result: passed.

## Files Changed

- `src/awf/service/logs.py`
- `tests/unit/service/test_logs_parts/test_logs_part_002.py`
- `plans/T17_SETUP_SECRET_REDACTION_CI_FIX_PLAN.md`
- `plans/T17_SETUP_SECRET_REDACTION_CI_FIX_VALIDATION.md`

## Residual Risk

No known gaps in the scoped repair. The old remote check run may still show
shard 7 failed until AWF pushes this local commit and GitHub re-runs CI.
