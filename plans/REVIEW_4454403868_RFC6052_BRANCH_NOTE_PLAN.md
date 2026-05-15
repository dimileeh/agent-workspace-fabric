# Review 4454403868 RFC 6052 Branch Note Plan

## Problem Statement and Scope

PR review comment `issue:4454403868` notes that
`_extract_nat64_embedded_ipv4_address` supports RFC 6052 NAT64 prefix lengths
`/32`, `/40`, `/48`, `/56`, and `/64`, but the current public translation
prefix set contains only the well-known `64:ff9b::/96` prefix. The non-`/96`
branches are therefore currently reserved for future translation prefixes, while
the locally-assigned `64:ff9b:1::/48` namespace is blocked before extraction.

Scope is limited to maintainer-facing documentation for the generic extractor
and focused validation that existing callback-target behavior remains unchanged.

## Requirements Checklist

- [ ] Explain why the non-`/96` RFC 6052 extraction branches exist despite being
  unreachable through the current translation prefix set.
- [ ] Preserve the current unconditional block for locally-assigned NAT64
  callback target addresses.
- [ ] Preserve existing well-known NAT64, 6to4, and private-address rejection
  behavior.
- [ ] Run focused callback target tests.

## Implementation Steps

1. Add a concise comment near `_NAT64_TRANSLATION_PREFIXES` or the extractor
   explaining that only `64:ff9b::/96` is active today and non-`/96` extraction
   support is retained for future explicitly opted-in translation prefixes.
2. Avoid behavioral changes to callback target publicness checks.
3. Run focused callback target tests.
4. Record validation results in
   `plans/REVIEW_4454403868_RFC6052_BRANCH_NOTE_VALIDATION.md`.

## Verification Commands and Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/common/test_callback_targets.py -q
```

Pass criteria: the focused callback target test module passes with only the
maintainer note and plan/validation docs changed.
