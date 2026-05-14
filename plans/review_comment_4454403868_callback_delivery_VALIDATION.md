# Review Comment 4454403868 Callback Delivery Validation

Plan reference: `review_comment_4454403868_callback_delivery_PLAN.md`

## Requirement Status

- Preserve validation of all resolved callback target addresses before delivery:
  Complete. `_validate_callback_target_dns` still validates every resolved
  address before returning a delivery tuple.
- Preserve fallback across multiple validated callback target addresses:
  Complete. Delivery still iterates across `connect_ip_addresses`; the regression
  test now proves IPv4-first fallback to IPv6 when the IPv4 attempt fails.
- Prefer IPv4 addresses before IPv6 addresses when both families resolve:
  Complete. `_callback_address_family_sort_key` orders validated IPv4 addresses
  first while retaining stable ordering within each address family.
- Emit a structured warning log for delivery-time target rejection:
  Complete. The `CALLBACK_TARGET_INVALID` branch now logs
  `callback.delivery_target_invalid` with delivery, subscription, event, source,
  workspace, operation, merge candidate, error code, and redacted error message.
- Keep target-invalid failures retryable through the existing repository path:
  Complete. The existing `mark_failed_or_retry` path is unchanged.
- Make pinned HTTP delivery explicitly pass no httpx extensions:
  Complete. `_httpx_post_json` now sets extensions from an explicit
  HTTPS-vs-non-HTTPS expression, and HTTP pinning is covered by a regression
  test.
- Add focused regression tests for the changed behavior:
  Complete. Tests cover IPv4-preferred fallback, target-invalid logging, and
  pinned HTTP extensions.
- Do not push, switch branches, or write any GitHub comment:
  Complete. No branch switching, push, or GitHub write was performed.

## Evidence

Files changed:

- `src/awf/service/callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/review_comment_4454403868_callback_delivery_PLAN.md`
- `plans/review_comment_4454403868_callback_delivery_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q -k 'pinned_http_request or prefers_ipv4 or private_delivery_target'`
  - First run failed as expected before implementation.
  - Second run passed: `3 passed, 15 deselected`.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: `18 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - First run found `SIM108`; implementation was adjusted.
  - Second run passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed: `Success: no issues found in 155 source files`.

## Gaps

None.
