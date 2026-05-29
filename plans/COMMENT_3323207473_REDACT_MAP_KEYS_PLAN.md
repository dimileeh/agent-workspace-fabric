# Comment 3323207473 Redact Map Keys Plan

## Problem Statement and Scope

The first-run rendering redactor sanitizes token-looking strings in mapping values, but mapping keys are stringified and preserved. Untrusted diagnostic details can therefore leak raw token-looking keys or provider references in JSON and pretty first-run output.

Scope is limited to `src/awf/host_setup/rendering.py` and focused rendering regression tests.

## Requirements Checklist

- Add a regression test proving token-looking and provider-reference mapping keys are redacted in both JSON and pretty first-run output.
- Preserve existing provider-ref key-name behavior where keys such as `credential_ref` and `provider_ref` redact their values.
- Keep tuple-preservation behavior for first-run redaction.
- Run only focused tests for the touched rendering behavior; broad AWF/GitHub validation remains owned by AWF after agent completion.

## Implementation Steps

1. Add a focused failing test in `tests/unit/service/test_host_setup_rendering.py`.
2. Update the rendering redactor so mapping keys pass through the same first-run key-safe redaction boundary before insertion.
3. Run the focused test file.
4. Create validation documentation with evidence and any gaps.
5. Commit the targeted fix locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
- Pass criteria: the focused rendering test file passes, including the new key-redaction regression.
