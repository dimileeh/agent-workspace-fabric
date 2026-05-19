# CLI URL Sanitizer Review Plan

## Problem Statement and Scope

PR review feedback flagged two display-side issues in request URL diagnostics:
IPv6 authority reconstruction and incomplete sensitive query parameter redaction.
The current implementation has moved URL helpers into `src/awf/common/urls.py`;
this task is scoped to that helper and focused regression tests.

## Requirements Checklist

- Verify IPv6 diagnostic URL sanitization preserves bracketed authorities,
  including when userinfo is redacted.
- Redact common sensitive query parameter aliases:
  `api_key`, `apikey`, `key`, `password`, `passwd`, and `auth`.
- Preserve existing behavior for normalized API URLs, relative URLs, and
  redacted userinfo.
- Do not weaken existing secret-leakage tests.

## Implementation Steps

1. Add focused failing tests in `tests/unit/common/test_urls.py`.
2. Run the focused tests to confirm the new redaction coverage fails before
   the implementation change.
3. Update `src/awf/common/urls.py` with the smallest redaction-key expansion.
4. Re-run focused tests and the relevant CLI/common validation checks.
5. Record validation results in `plans/cli_url_sanitizer_review_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_urls.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/common/test_urls.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/urls.py tests/unit/common/test_urls.py`
  passes.
