# Review Level 4454303511 Plan

## Problem Statement and Scope

Address the review-level feedback for PR comment `issue:4454303511` in setup
dependency network failure classification. The scope is limited to
`src/awf/runtime/validation.py` and focused regression tests for:

- avoiding fallback host false positives for common archive/config artifact
  suffixes;
- allowing retryable HTTP 5xx dependency failures whose response body contains
  non-auth uses of "forbidden";
- preserving deterministic suppression for explicit 403/Forbidden auth-style
  failures.

## Requirements Checklist

- Add regression coverage for artifact-like fallback host candidates such as
  `.tar.gz`, `.whl`, `.cfg`, `.yml`, `.yaml`, and `.json`.
- Add regression coverage that a dependency HTTP 503 failure mentioning
  "temporarily forbidden" remains retryable.
- Add regression coverage that an explicit HTTP 403 Forbidden failure remains
  deterministic and is not classified as retryable.
- Update deterministic/fallback matching narrowly without weakening existing
  redaction or retry-budget behavior.
- Run the narrow unit tests that cover the changed classifier behavior.

## Implementation Steps

1. Add focused tests in `tests/unit/runtime/test_validation.py`.
2. Confirm the new tests fail against the current classifier behavior.
3. Update `src/awf/runtime/validation.py` to extend fallback suffix exclusion
   and narrow the deterministic `forbidden` match to explicit HTTP 403
   contexts.
4. Re-run the focused tests, then run the broader validation unit module if
   practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "setup_dependency_network_classifier"`
  passes.
- If runtime permits, `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
