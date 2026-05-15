# Review Thread PRRT_kwDOSJAM6s6CXnpW Validation

Plan reference: `plans/review_thread_PRRT_kwDOSJAM6s6CXnpW_PLAN.md`

## Requirement Status

- Add a regression for IPv4-translated callback targets under
  `::ffff:0:0:0/96`: Complete.
- Reject IPv4-translated callback target literals before relying on IPv6
  `is_global`: Complete.
- Cover both registration-time host literals and delivery-time resolved IP
  validation: Complete.
- Preserve existing IPv4-mapped, IPv4-compatible, 6to4, NAT64, and ordinary
  public callback target behavior: Complete.
- Run focused callback target policy tests and static checks for the changed
  code: Complete.

## Evidence

Files changed:

- `src/awf/common/callback_targets.py`
- `tests/unit/common/test_callback_targets.py`
- `tests/unit/api/test_callbacks.py`
- `tests/unit/service/test_callbacks.py`
- `plans/review_thread_PRRT_kwDOSJAM6s6CXnpW_PLAN.md`
- `plans/review_thread_PRRT_kwDOSJAM6s6CXnpW_VALIDATION.md`

Commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rejects_internal_target_hosts_without_insert -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_rejects_translated_delivery_target_that_embeds_private_ipv4 -q
uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py
uv run --python 3.12 --extra dev mypy src/awf/common/callback_targets.py
```

Results:

- The common callback target regression failed before implementation on
  `::ffff:0:169.254.169.254` and `::ffff:0:8.8.8.8`.
- Common callback target tests passed: 32 tests.
- Registration invalid-host regression passed: 21 tests.
- Delivery resolved-IP regression passed: 4 tests.
- Ruff passed.
- Mypy passed for `src/awf/common/callback_targets.py`.
