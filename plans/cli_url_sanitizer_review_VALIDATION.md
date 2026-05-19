# CLI URL Sanitizer Review Validation

Plan reference: `plans/cli_url_sanitizer_review_PLAN.md`

## Requirement Status

- Complete: Verify IPv6 diagnostic URL sanitization preserves bracketed
  authorities, including when userinfo is redacted.
  - Evidence: Added
    `test_sanitize_request_url_preserves_ipv6_authority_when_userinfo_is_redacted`
    in `tests/unit/common/test_urls.py`.
- Complete: Redact common sensitive query parameter aliases:
  `api_key`, `apikey`, `key`, `password`, `passwd`, and `auth`.
  - Evidence: Added parametrized regression coverage in
    `tests/unit/common/test_urls.py` and expanded `_SENSITIVE_QUERY_KEYS` in
    `src/awf/common/urls.py`.
- Complete: Preserve existing behavior for normalized API URLs, relative URLs,
  and redacted userinfo.
  - Evidence: Existing URL-helper tests still pass.
- Complete: Do not weaken existing secret-leakage tests.
  - Evidence: Existing CLI and common URL tests were preserved and passed.

## Red Phase Evidence

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_urls.py -q`
  failed before implementation with six failures for unredacted `api_key`,
  `apikey`, `key`, `password`, `passwd`, and `auth` query values.

## Verification Commands

- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/common/test_urls.py -q`
  (`18 passed`)
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py tests/unit/common/test_urls.py -q`
  (`135 passed`)
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/common/urls.py tests/unit/common/test_urls.py`

## Remaining Gaps

None.
