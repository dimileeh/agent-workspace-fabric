# Request Admission Prune Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CZJal_REQUEST_ADMISSION_PRUNE_PLAN.md`

## Requirement Status

- Verify no existing class state already avoids repeated same-window pruning:
  Complete. `RequestAdmissionLimiter` previously had only `_clock` and
  `_buckets`; no prune window tracking existed.
- Add a regression test proving repeated admits in the same window do not
  rescan existing buckets: Complete. Added
  `test_request_admission_limiter_prunes_once_per_window`.
- Prune stale buckets when the relevant `window_seconds` advances: Complete.
  The regression advances the clock and asserts stale window buckets are gone.
- Preserve existing admission decisions, metadata, and validation errors:
  Complete. Full `tests/unit/api/test_deps.py` still passes.
- Keep changes scoped to request admission code and focused tests: Complete.
  Code changes are limited to `src/awf/api/request_admission.py` plus the
  focused unit test and plan/validation files.

## Evidence

- New regression failed before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_limiter_prunes_once_per_window -q`
  failed with `iterated_keys == 26`.
- Focused regression after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py::test_request_admission_limiter_prunes_once_per_window -q`
  passed.
- Touched API unit tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_deps.py -q`
  passed, `22 passed`.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/api/request_admission.py tests/unit/api/test_deps.py`
  passed.
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`
  passed.

## Remaining Gaps

None.
