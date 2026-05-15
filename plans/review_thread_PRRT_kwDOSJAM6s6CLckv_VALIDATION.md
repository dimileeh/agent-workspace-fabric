# Review Thread PRRT_kwDOSJAM6s6CLckv Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CLckv_PLAN.md`

## Requirement Status

- Preserve existing callback URL policy checks: Complete
  - Existing validation branches remain in `src/awf/service/callbacks.py`.
  - `tests/unit/service/test_callbacks.py` still covers private DNS rejection,
    HTTPS-only enforcement, and allowlist enforcement.
- Return the validated DNS address from delivery-time validation: Complete
  - `_validate_callback_target` now returns `ValidatedCallbackTarget`.
  - `_validate_callback_target_dns` returns the first validated public address.
- Ensure the default HTTP poster connects to the validated address while preserving
  original Host/SNI: Complete
  - `_httpx_post_json` rewrites the request URL to the validated IP address and
    supplies the original Host header plus HTTPS `sni_hostname`.
  - Regression test:
    `test_default_httpx_poster_pins_connection_to_validated_callback_address`.
- Keep injected test/custom posters compatible with the delivery service contract:
  Complete
  - `CallbackHttpPoster` accepts `connect_ip_address`.
  - `test_successful_delivery_posts_sanitized_json_and_marks_succeeded` asserts the
    service passes the validated address to the poster.
- Add regression coverage for the DNS rebinding issue: Complete
  - Initial focused test run failed on the new pinned-address assertions before the
    implementation.
  - Final focused test run passed after implementation.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/review_thread_PRRT_kwDOSJAM6s6CLckv_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6CLckv_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Initial result before implementation: failed with the new regression assertions.
  - Final result: `14 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/callbacks.py`
  - Result: passed.

## Gaps

No gaps found.
