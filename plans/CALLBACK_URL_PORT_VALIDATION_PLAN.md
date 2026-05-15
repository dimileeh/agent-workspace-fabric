# Callback URL Port Validation Plan

## Problem Statement And Scope

Callback subscription `target_url` validation currently checks scheme, host, userinfo,
fragment, and public-host policy, but does not force `urllib.parse.urlsplit`
to validate the URL port. Malformed or out-of-range ports can be accepted during
registration and then fail later when delivery helpers read `parsed.port`.

Scope is limited to rejecting invalid callback target ports at registration and
in the service-side static policy used for stored rows.

## Requirements Checklist

- Add regression coverage for callback registration rejecting malformed and
  out-of-range target URL ports.
- Add regression coverage for service-side callback target policy rejecting the
  same invalid ports before DNS or delivery.
- Preserve existing valid callback URL behavior, including explicit valid ports.
- Keep the change scoped to callback URL validation.

## Implementation Steps

1. Add failing unit tests for invalid callback `target_url` ports in the API and
   service callback validation test suites.
2. Update callback URL validation to access and validate `parsed.port`, catching
   `ValueError` from malformed or out-of-range ports and raising the existing
   validation error style.
3. Run focused callback tests.
4. Run lint and any narrow static checks needed for touched files.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas.py src/awf/service/callbacks.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py`
  must pass.
