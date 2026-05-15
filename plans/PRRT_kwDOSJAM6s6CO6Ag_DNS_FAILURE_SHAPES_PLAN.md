# PRRT_kwDOSJAM6s6CO6Ag DNS Failure Shapes Plan

## Problem Statement And Scope

The setup dependency network classifier should treat transient dependency-install DNS
failures reported as `getaddrinfo ENOTFOUND ...` or `Could not resolve host ...` as
retryable setup dependency network failures. The current classifier recognizes other
DNS spellings, so these equivalent npm/git/curl fetch failures can miss the bounded
setup retry.

Scope is limited to the setup dependency network classifier and focused regression
tests for the review thread.

## Requirements Checklist

- Add failing regression coverage for npm registry DNS failures reported with
  `ENOTFOUND`.
- Add failing regression coverage for git/curl-backed dependency fetch DNS failures
  reported with `Could not resolve host`.
- Classify both forms as `SETUP_DEPENDENCY_NETWORK_FAILURE` with transient category
  `dns` for recognized dependency setup commands.
- Preserve existing deterministic-failure filtering and setup-context safeguards.

## Implementation Steps

1. Add targeted unit tests in `tests/unit/runtime/test_validation.py` for the missing
   DNS spellings.
2. Run the new focused tests and confirm they fail before implementation.
3. Extend the DNS transient pattern in `src/awf/runtime/validation.py` narrowly.
4. Run the focused tests, then the runtime validation unit tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- Focused tests fail before the implementation and pass after the implementation.
