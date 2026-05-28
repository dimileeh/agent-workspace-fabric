# COMMENT 3320255224 Secret-Shaped Mapping Keys Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FgLjk` reports that host setup config secret scanning rejects reserved mapping key names such as `token`, but accepts mapping keys that look like raw secrets. Because provider and client names are arbitrary mapping keys, `write_host_setup_config` can serialize a raw token as a YAML key even when values are otherwise safe references.

Scope is limited to `src/awf/host_setup/config.py`, focused host setup config tests, and this plan/validation evidence.

## Requirements Checklist

- Add a regression test proving a secret-shaped provider mapping key is rejected before serialization.
- Apply the existing secret-like string detection to string mapping keys.
- Keep rejection diagnostics sanitized so raw secret-shaped key text is not surfaced in error payloads.
- Preserve existing secret key/value behavior and reason-coded host setup config errors.
- Use focused validation only; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add a failing unit test in `tests/unit/service/test_host_setup_config.py` for a provider key shaped like a raw GitHub token.
2. Update `_ensure_no_secret_payload` to reject string mapping keys that `_looks_like_secret_value`.
3. Use a redacted path segment for secret-like mapping keys so diagnostics identify location without echoing secret material.
4. Run the focused host setup config test file.
5. Record validation evidence in `plans/COMMENT_3320255224_SECRET_KEY_SHAPE_VALIDATION.md`.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q
```

Pass criteria: the focused test file passes, including the new regression. Full AWF/GitHub validation is intentionally not run during this agent phase.
