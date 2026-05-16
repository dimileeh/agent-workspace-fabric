# PR259 CI Failures Plan

## Problem Statement And Scope

PR #259 is failing the Python full coverage CI job because focused unit tests fail in two areas:

- workspace refresh idempotency/coalescing creates a second active operation instead of returning the already-active operation response;
- `docs/REASON_CATALOG.md` is out of sync with `src/awf/service/doctor/reasons.py` and its headings are no longer discoverable by the catalog coverage test.

Scope is limited to the failing CI surfaces, their regression coverage, and required plan/validation documentation.

## Requirements Checklist

- [ ] Preserve AWF workspace contract: do not switch branches, push, rebase, or disable checks.
- [ ] Reproduce the provided focused failures before changing implementation.
- [ ] Fix refresh endpoint behavior so concurrent/active refresh requests coalesce to the existing active operation response.
- [ ] Fix the reason catalog drift using the repository generator or a compatible generator fix.
- [ ] Run the focused repro command until all three reported tests pass.
- [ ] Run narrow adjacent validation for touched files when practical.
- [ ] Record implementation validation in `plans/PR259_CI_FAILURES_VALIDATION.md`.
- [ ] Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Inspect the refresh endpoint/service code and the failing idempotency test to identify where active-operation coalescing is bypassed.
2. Add or adjust regression coverage first if the existing failing test does not precisely lock the intended behavior.
3. Implement the smallest behavior change needed to return the existing active refresh operation for coalesced requests.
4. Inspect reason catalog generation and documentation heading expectations.
5. Regenerate or correct `docs/REASON_CATALOG.md` so synchronized generation and coverage scanning agree.
6. Run focused and adjacent validation commands.
7. Write the validation document and commit all scoped changes.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency.py::test_refresh_endpoint_returns_operation_response_and_coalesces_active_request tests/unit/service/test_doctor_reasons.py::test_reason_catalog_is_synchronized_with_python_source tests/unit/docs/test_catalog_coverage.py::test_catalog_coverage -q`
  - Pass criteria: all three CI-reported tests pass.
- Adjacent commands selected after inspecting touched code.
  - Pass criteria: tests covering the touched API/service/docs surfaces pass without weakening assertions.
