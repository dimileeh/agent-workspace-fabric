# Address PRRT_kwDOSJAM6s6GOmMW Validation

Plan reference: `ADDRESS_THREAD_PRRT_kwDOSJAM6s6GOmMW_PLAN.md`

## Requirement Status

- Add a regression test for a missing-image inspect error classified as
  `DOCKER_UNAVAILABLE`: Complete. Added
  `test_companion_image_exists_treats_docker_unavailable_no_such_image_as_missing`
  in `tests/unit/node/test_companion_images.py`.
- Preserve propagation of genuine Docker probe failures: Complete. Existing
  `test_companion_image_exists_preserves_probe_error_reason_code` still covers a
  real daemon connectivity failure and remains passing.
- Keep confirmed missing-image inspect failures returning `False` so callers can
  clear the companion image and fall back to inline build: Complete.
  `_is_missing_image_inspect_failure` now treats "no such image" as confirmed
  missing even when the error was classified as `DOCKER_UNAVAILABLE`.
- Avoid broad AWF/GitHub-owned validation; run only focused local checks:
  Complete. Full AWF/GitHub validation is left to AWF after agent completion.

## Evidence

- Files changed:
  - `src/awf/node/companion_images.py`
  - `tests/unit/node/test_companion_images.py`
  - `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GOmMW_PLAN.md`
  - `plans/ADDRESS_THREAD_PRRT_kwDOSJAM6s6GOmMW_VALIDATION.md`
- Regression-first evidence:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py::test_companion_image_exists_treats_docker_unavailable_no_such_image_as_missing -q`
    failed before the production change because `DOCKER_UNAVAILABLE` was raised.
- Passing checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_images.py -q`
    passed: 22 tests.
  - `uv run --python 3.12 --extra dev ruff check src/awf/node/companion_images.py tests/unit/node/test_companion_images.py`
    passed.
