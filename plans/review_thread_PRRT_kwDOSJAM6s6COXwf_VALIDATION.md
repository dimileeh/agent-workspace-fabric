# Review Thread PRRT_kwDOSJAM6s6COXwf Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6COXwf_PLAN.md`

## Requirement Status

- Explicitly recognize the RFC 8215 local-use NAT64 namespace
  `64:ff9b:1::/48`: Complete. `callback_targets.py` now defines
  `_NAT64_LOCAL_USE_PREFIX`.
- Re-check the embedded IPv4 address for local-use NAT64 callback targets using
  the same publicness policy applied to well-known NAT64 targets: Complete.
  `_callback_target_public_address` unwraps both configured NAT64 translation
  prefixes to their embedded IPv4 address before the publicness check.
- Reject local-use NAT64 callback targets that embed private, link-local, or
  otherwise non-public IPv4 addresses: Complete. The regression asserts
  `64:ff9b:1::c0a8:0101` is rejected after IPv4 unmasking.
- Preserve support for local-use NAT64 callback targets that embed public IPv4
  addresses: Complete. The regression asserts `64:ff9b:1::0808:0808` is
  accepted after IPv4 unmasking.
- Keep the change narrowly scoped to callback target policy: Complete. Only the
  shared callback target policy, its focused unit tests, and required
  plan/validation documents changed.

## Evidence

Files changed:

- `src/awf/common/callback_targets.py`
- `tests/unit/common/test_callback_targets.py`
- `plans/review_thread_PRRT_kwDOSJAM6s6COXwf_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6COXwf_VALIDATION.md`

Commands run:

- Initial TDD failure confirmed:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py::test_locally_assigned_nat64_callback_targets_unmask_embedded_ipv4 -q`
- Focused regression passes after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py::test_locally_assigned_nat64_callback_targets_unmask_embedded_ipv4 -q`
- Callback target unit surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py`
- Type check:
  `uv run --python 3.12 --extra dev mypy src/awf`

## Remaining Gaps

None.
