# Review Thread PRRT_kwDOSJAM6s6COXwf Plan

## Problem Statement And Scope

PR review feedback reports that callback target validation explicitly unmasks
the NAT64 well-known prefix `64:ff9b::/96`, but does not explicitly cover the
RFC 8215 local-use NAT64 namespace `64:ff9b:1::/48`. The policy should not rely
on the Python `ipaddress` reserved/global classification for this range because
the SSRF guard depends on inspecting the embedded IPv4 target.

Scope is limited to shared callback target validation in
`src/awf/common/callback_targets.py` and focused regression coverage in
`tests/unit/common/test_callback_targets.py`.

## Requirements Checklist

- Explicitly recognize the RFC 8215 local-use NAT64 namespace
  `64:ff9b:1::/48`.
- Re-check the embedded IPv4 address for local-use NAT64 callback targets using
  the same publicness policy applied to well-known NAT64 targets.
- Reject local-use NAT64 callback targets that embed private, link-local, or
  otherwise non-public IPv4 addresses.
- Preserve support for local-use NAT64 callback targets that embed public IPv4
  addresses.
- Keep the change narrowly scoped to callback target policy.

## Implementation Steps

1. Add a focused regression test for local-use NAT64 addresses with public and
   private embedded IPv4 targets.
2. Confirm the new regression fails before implementation.
3. Add the RFC 8215 local-use NAT64 prefix to the explicit NAT64 unmasking path.
4. Re-run the focused regression and the callback target unit test module.

## Verification Commands And Pass Criteria

- Initial TDD command:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py::test_locally_assigned_nat64_callback_targets_unmask_embedded_ipv4 -q`
  should fail before implementation.
- Focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py::test_locally_assigned_nat64_callback_targets_unmask_embedded_ipv4 -q`
  must pass after implementation.
- Callback target unit surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  must pass.
- Lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py`
  must pass.
