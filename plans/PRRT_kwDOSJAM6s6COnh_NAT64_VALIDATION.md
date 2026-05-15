# PRRT_kwDOSJAM6s6COnh NAT64 Callback Target Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6COnh_NAT64_PLAN.md`

## Requirement Status

- Complete: Decode `64:ff9b::/96` callback targets exactly as before.
  Evidence: `src/awf/common/callback_targets.py` preserves `/96` extraction
  from the last 32 bits; existing well-known NAT64 common and delivery tests
  pass.
- Complete: Decode `64:ff9b:1::/48` callback targets using the RFC 6052
  embedded IPv4 layout.
  Evidence: `_extract_nat64_embedded_ipv4_address` removes the reserved `u`
  octet for non-`/96` prefixes and extracts the IPv4 bytes after the matched
  prefix.
- Complete: Reject local-use NAT64 targets whose embedded IPv4 address is
  private, even when suffix bits look public.
  Evidence: `tests/unit/common/test_callback_targets.py` covers
  `64:ff9b:1:a00:0:100:808:808`; `tests/unit/service/test_callbacks.py`
  covers the same resolved delivery target.
- Complete: Preserve public local-use NAT64 targets whose embedded IPv4 address
  is public.
  Evidence: `tests/unit/common/test_callback_targets.py` covers
  `64:ff9b:1:808:8:800::`.
- Complete: Run focused regression tests and lint for the touched files.
  Evidence: verification commands below passed after implementation and
  formatting.
- Complete: Commit the fix locally without switching branches or pushing.
  Evidence: this validation was written before staging/commit; no branch switch
  or push was performed.

## Verification Commands

- Pre-fix TDD check:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  failed as expected with 1 failure and 21 passing tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q`
  passed: 22 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py::test_drain_due_rejects_nat64_delivery_target_that_embeds_private_ipv4 -q`
  passed: 24 tests.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py tests/unit/api/test_callbacks.py tests/unit/service/test_callbacks.py -q`
  passed: 105 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/common/callback_targets.py tests/unit/common/test_callback_targets.py tests/unit/service/test_callbacks.py`
  passed after applying `ruff format` to the touched files.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passed.
