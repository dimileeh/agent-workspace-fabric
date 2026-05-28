# Secret Payload Sequence Traversal Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6Ffn8T` reports that host setup config secret payload scanning traverses Pydantic models, mappings, and strings, but skips list and tuple values. A skipped sequence can hide secret-like string values or nested secret-bearing keys before the config is read or written.

Scope is limited to `src/awf/host_setup/config.py` and focused host setup config regression tests.

## Requirements Checklist

- Add a regression test proving sequence-contained secret-like values are rejected.
- Add a regression test proving mappings nested inside sequences are still scanned for secret-bearing keys.
- Update `_ensure_no_secret_payload` to recursively inspect list and tuple elements.
- Preserve sanitized error reporting without leaking secret values.
- Run focused host setup config tests only. Full AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add failing regression coverage in `tests/unit/service/test_host_setup_config.py`.
2. Run the focused regression test and confirm the current implementation fails.
3. Update `_ensure_no_secret_payload` to recurse into list and tuple elements with path context.
4. Re-run the focused host setup config test file.
5. Record validation evidence in `plans/SECRET_PAYLOAD_SEQUENCES_VALIDATION.md`.
