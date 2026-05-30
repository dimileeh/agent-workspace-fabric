# Review PRRT_kwDOSJAM6s6Foreh Redacted Key Collisions Plan

## Problem Statement and Scope

The first-run redaction pipeline redacts sensitive mapping keys before inserting
them into a plain dict. Distinct secret-looking keys can therefore collapse to
the same rendered key, such as `[redacted]`, and the later value silently
overwrites the earlier value. The fix should preserve diagnostic values without
revealing the original sensitive keys.

Scope is limited to first-run rendering redaction behavior, focused regression
coverage, and this plan/validation record.

## Requirements Checklist

- Add a regression test proving colliding redacted mapping keys retain every
  redacted entry instead of overwriting earlier values.
- Preserve key redaction; raw token-like keys and provider-reference keys must
  not appear in rendered JSON or pretty output.
- Keep provider-reference metadata keys such as `credential_ref(s)` and
  `provider_ref(s)` redacted to the redaction marker value.
- Avoid broad validation; run only focused tests/checks for the touched files.

## Implementation Steps

1. Add a focused failing regression test in
   `tests/unit/service/test_host_setup_rendering.py`.
2. Update `_redact_provider_refs` so redacted mapping-key collisions receive a
   deterministic non-secret disambiguator instead of overwriting prior entries.
3. Run the focused rendering test(s) and lint/type checks for the touched files.
4. Record validation evidence in the matching validation document.
