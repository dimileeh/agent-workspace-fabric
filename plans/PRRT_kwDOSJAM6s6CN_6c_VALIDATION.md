# PRRT_kwDOSJAM6s6CN_6c Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6CN_6c_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving a resolved NAT64 address that embeds
  a non-public IPv4 address is rejected before any callback POST is attempted.
  Evidence: `tests/unit/service/test_callbacks.py` adds
  `test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4`.
- Complete: Preserve existing callback behavior for ordinary public IPv4 and
  IPv6 targets. Evidence: existing callback service tests still pass.
- Complete: Keep the change localized to callback target validation code.
  Evidence: implementation only changes `_is_public_ip` in
  `src/awf/service/callbacks.py`.
- Complete: Validate with the narrowest relevant test command. Evidence: the
  focused regression failed before the implementation because the POST was
  attempted, then passed after the implementation.
- Complete: Record implementation validation in this document.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 -q`
  - Before implementation: failed with one poster call to
    `64:ff9b::a9fe:a9fe`.
  - After implementation: passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py -q`
  - Passed: 25 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/callbacks.py tests/unit/service/test_callbacks.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/callbacks.py`
  - Passed.
