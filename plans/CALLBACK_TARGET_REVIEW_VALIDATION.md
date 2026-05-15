# Callback Target Review Fix Validation

Plan reference: `plans/CALLBACK_TARGET_REVIEW_PLAN.md`

## Requirement Status

- Complete: Classify callback target validation timeouts as
  `CALLBACK_TARGET_INVALID`.
  Evidence: `src/awf/service/callbacks.py` now wraps validation timeout in
  `ValueError`; `tests/unit/service/test_callbacks.py` asserts the
  target-invalid delivery record and log event.
- Complete: Reject 6to4 callback target host literals at registration-time host
  validation.
  Evidence: `src/awf/common/callback_targets.py` explicitly blocks
  `2002::/16`; `tests/unit/api/test_callbacks.py` covers registration
  rejection.
- Complete: Reject 6to4 addresses returned by delivery-time DNS resolution.
  Evidence: delivery DNS publicness now uses
  `is_public_callback_target_ip`; `tests/unit/service/test_callbacks.py`
  covers a resolved 6to4 address.
- Complete: Preserve existing handling for public hosts, IPv4-mapped IPv6,
  NAT64, and request/post failures.
  Evidence: common publicness tests include public IPv4/IPv6, IPv4-mapped
  private IPv6, NAT64 private IPv4, and 6to4; affected service tests pass.
- Complete: Commit the fix locally without switching branches or pushing.
  Evidence: this validation was written before staging/commit; no branch switch
  or push was performed.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py::test_register_callback_rejects_internal_target_hosts_without_insert tests/unit/service/test_callbacks.py::test_drain_due_marks_callback_target_validation_timeout_as_target_invalid tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 tests/unit/service/test_callbacks.py::test_drain_due_rejects_6to4_delivery_target -q`
  passed: 44 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py -q`
  passed: 101 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py src/awf/service/callbacks.py tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/common/callback_targets.py src/awf/service/callbacks.py tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
