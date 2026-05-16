# Review Thread PRRT_kwDOSJAM6s6CXnpW Plan

## Problem Statement And Scope

Address the unresolved PR review thread on
`src/awf/common/callback_targets.py`. The review reports that callback target
validation can accept IPv4-translated IPv6 literals in
`::ffff:0:0:0/96` because Python 3.12 marks addresses such as
`::ffff:0:169.254.169.254` as global and the current policy only handles
IPv4-mapped, IPv4-compatible, 6to4, and NAT64 cases.

Scope is limited to the shared callback target host/IP policy and focused unit
coverage.

## Requirements Checklist

- Add a regression for IPv4-translated callback targets under
  `::ffff:0:0:0/96`.
- Reject IPv4-translated callback target literals before relying on IPv6
  `is_global`.
- Cover both registration-time host literals and delivery-time resolved IP
  validation.
- Preserve existing IPv4-mapped, IPv4-compatible, 6to4, NAT64, and ordinary
  public callback target behavior.
- Run focused callback target policy tests and static checks for the changed
  code.

## Implementation Steps

1. Add failing common-policy unit cases for IPv4-translated private and public
   callback target literals, plus focused registration/delivery regressions for
   the metadata-address form.
2. Update `src/awf/common/callback_targets.py` to explicitly block the
   IPv4-translated prefix.
3. Re-run the focused tests and lint/type checks for the touched Python code.
4. Record validation evidence in
   `plans/review_thread_PRRT_kwDOSJAM6s6CXnpW_VALIDATION.md`.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_callbacks.py::test_register_callback_rejects_internal_target_hosts_without_insert -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_callbacks.py::test_drain_due_rejects_translated_delivery_target_that_embeds_private_ipv4 -q
uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py
uv run --python 3.12 --extra dev mypy src/awf/common/callback_targets.py
```

Pass criteria: the new regression fails before the implementation change, then
all listed focused verification commands pass after the fix.
