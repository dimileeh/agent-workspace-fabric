# NAT64 Local-Use Callback Targets Validation

Plan reference: `plans/NAT64_LOCAL_USE_CALLBACK_TARGETS_PLAN.md`

## Requirement Status

- Block callback target IPs in `64:ff9b:1::/48` unconditionally: Complete.
- Preserve existing well-known `64:ff9b::/96` NAT64 embedded IPv4 checks: Complete.
- Add a regression for the reported `64:ff9b:1:c001::c0a8:0101` bypass: Complete.
- Keep the change focused and avoid unrelated callback delivery behavior: Complete.
- Validate with focused tests for callback target policy and delivery rejection: Complete.

## Evidence

Files changed:

- `src/awf/common/callback_targets.py`
- `tests/unit/common/test_callback_targets.py`
- `tests/unit/service/test_callbacks.py`
- `plans/NAT64_LOCAL_USE_CALLBACK_TARGETS_PLAN.md`
- `plans/NAT64_LOCAL_USE_CALLBACK_TARGETS_VALIDATION.md`

Commands run:

- Failing regression before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 -q`
  - Result: failed on `64:ff9b:1:c001::c0a8:0101` and previously-public local-use NAT64 examples.
- Passing focused validation after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 -q`
  - Result: 27 passed.
- Passing common callback target module validation:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  - Result: 24 passed.
- Passing lint validation:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py`
  - Result: all checks passed.

## Remaining Gaps

None.
