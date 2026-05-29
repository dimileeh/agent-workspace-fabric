# Slack Token Prefix Redaction Plan

## Problem Statement and Scope

An unresolved PR review thread reports that rejected first-run config values
starting with shortened Slack token prefixes such as `xoxb-a` or `xoxp-` can be
rendered without redaction. Host setup config rejects those prefixes as
secret-like input, but the shared known-token pattern only redacts Slack tokens
when at least eight suffix characters are present.

Scope is limited to the shared token pattern and focused regression coverage for
the common redactors that consume it.

## Requirements Checklist

- Redact shortened Slack token-looking values rejected by host setup config,
  including `xoxb-a` and `xoxp-`.
- Keep the fix in the shared token pattern so audit, log, and first-run
  redactors remain aligned.
- Add focused regression tests before implementation.
- Run only targeted local checks; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add shared-token-pattern regression coverage for truncated Slack token
   prefixes.
2. Confirm the new regression fails against the current pattern.
3. Relax the shared Slack-token suffix matcher to include shortened rejected
   prefixes.
4. Re-run the focused regression tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_token_patterns.py -q`
  - Passes after implementation and demonstrates the shared redactors catch the
    shortened Slack prefixes.

Full AWF/GitHub validation is managed after agent completion and is intentionally
not run in this workspace fix cycle.
