# Review Thread PRRT_kwDOSJAM6s6CLRNx Plan

## Problem Statement And Scope

The setup dependency network classifier currently treats any standalone `5xx`
number as an `http_5xx` transient. In dependency-looking commands such as
`uv sync`, unrelated numbers like exit code `512` can therefore trigger a false
retry.

Scope is limited to the `http_5xx` transient pattern and focused regression
coverage for the classifier.

## Requirements Checklist

- Add a regression test showing an unrelated standalone `5xx` number in setup
  output is not classified as a transient HTTP failure.
- Preserve classification for explicit HTTP 5xx response/status output.
- Narrow the `http_5xx` regex so it requires HTTP/status language or existing
  server-error phrases.
- Run the narrow validation test for the changed classifier.

## Implementation Steps

1. Add classifier regression tests in `tests/unit/runtime/test_validation.py`.
2. Confirm the unrelated `512` regression fails with the current implementation.
3. Update `_SETUP_TRANSIENT_PATTERNS` in `src/awf/runtime/validation.py`.
4. Re-run the focused tests and update validation evidence.
