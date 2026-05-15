# CI Callback Reason Catalog and Coverage Plan

## Problem Statement and Scope

PR #249 fails the `python-full-coverage` GitHub Actions job. The failed log shows:

- `tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage` reports missing public reason catalog entries for `CALLBACK_TARGET_INVALID` and `CALLBACK_TARGET_VALIDATION_TIMEOUT`.
- The same full-coverage run ends below the 99% repository threshold, with the newly changed callback delivery service showing uncovered branches.

Scope is limited to fixing the real CI failure without weakening or skipping checks.

## Requirements Checklist

- [x] Add public catalog documentation for `CALLBACK_TARGET_INVALID`.
- [x] Add public catalog documentation for `CALLBACK_TARGET_VALIDATION_TIMEOUT`.
- [x] Add focused regression tests that exercise uncovered callback delivery branches introduced by PR #249.
- [x] Add adjacent edge-case regression coverage needed to restore the full-suite 99% threshold after the callback-focused fixes.
- [x] Preserve callback behavior; do not loosen target validation, retry semantics, auth, or coverage checks.
- [x] Run focused local validation for the catalog and callback coverage changes.
- [x] Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Reproduce the catalog coverage failure locally with the narrow docs test.
2. Inspect callback delivery branch coverage from `src/awf/service/callbacks.py` and existing callback service tests.
3. Update `docs/REASON_CATALOG.md` with operator-facing entries for the two callback target validation reason codes.
4. Add focused unit tests for uncovered callback delivery target validation and posting helper branches.
5. Run targeted tests with coverage for the touched callback service and the catalog test.
6. Run the repo’s narrow lint/type checks relevant to changed files, plus any broader verification justified by the touched surface.

## Scope Update

After the catalog entries and callback service coverage were fixed, the full CI-equivalent coverage command still reported total coverage below 99%. The implementation therefore added small edge-case tests in existing coverage-edge test modules for runtime CI evidence parsing, readiness evidence formatting, failure causality dialect handling, executor validation evidence payloads, PR monitor recovery payloads, and worker scheduling/release helpers. No production behavior was changed.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/docs/test_catalog_coverage.py -q`
  - Passes with no missing catalog entries.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py --cov=awf.service.callbacks --cov-report=term-missing --cov-fail-under=99 -q`
  - Passes and confirms callback service coverage is no longer the obvious full-suite coverage drag.
- `uv run --python 3.12 --extra dev ruff check docs/REASON_CATALOG.md tests/unit/service/test_callbacks.py`
  - Passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passes if test/code changes require type confidence beyond docs-only catalog updates.
