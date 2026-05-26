# Plan: Address PR #286 review comment 4366409886

## Problem statement and scope
A review-level bugbot comment on PR #286 indicates issues in the supply-chain policy helper refactor. Scope is limited to `src/awf/service/supply_chain_policy_helpers.py` and the related supply-chain policy tests.

## Requirements checklist
1. Restore URL credential redaction behavior by fixing the regex used in `_URL_CREDENTIAL_PATTERN`.
2. Add regression coverage proving credentials with `s` in user/password are redacted in command excerpts.
3. Keep changes narrowly scoped to avoid altering broader policy behavior.
4. Run focused verification for the modified behavior only.

## Implementation steps
- Update `_URL_CREDENTIAL_PATTERN` in `src/awf/service/supply_chain_policy_helpers.py` to use the correct whitespace class.
- Add a unit test in `tests/unit/service/test_supply_chain_policy.py` for credentialed registry URLs that include `s` in authority credentials.
- Confirm no other behavior changes are introduced.

## Verification commands and pass criteria
- Run targeted pytest cases touching supply-chain command redaction:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_supply_chain_policy.py -k redacts`
- Expected:
  - Added regression test passes.
  - Existing targeted redaction test(s) continue to pass.
