# Comment 4396899384 Cursor Static Token Warning Plan

## Problem Statement and Scope

PR review comment `4396899384` reports that Cursor readiness treats
`CURSOR_API_KEY` as static service-environment auth but returns no
`STATIC_TOKEN_FALLBACK` warning. Codex, Claude Code, and Gemini already emit
that warning for equivalent env-token auth.

Scope is limited to Cursor provider readiness warning parity and a focused
regression test. The provider inference concern included in the review summary
is already covered by current `provider_failures` code and tests, so it is not
part of this code change.

## Requirements Checklist

- Cursor env auth keeps returning `CURSOR_ENV_AUTH_PRESENT` with the existing
  credential scope and isolation metadata.
- Cursor env auth emits a `STATIC_TOKEN_FALLBACK` warning using the same warning
  structure and wording pattern as other provider env-auth paths.
- Secret values remain redacted from readiness payloads.
- Focused tests demonstrate the new Cursor warning behavior.
- Broad AWF/GitHub validation is left to AWF after agent completion.

## Implementation Steps

1. Update the Cursor readiness regression test to expect the static-token
   warning for `CURSOR_API_KEY`.
2. Run the focused test and confirm it fails on the current implementation.
3. Update `_check_cursor()` to include `_security_warning(...)` in the env-auth
   return path.
4. Re-run the focused test, plus a narrow provider-readiness test slice if
   needed.
5. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q`

Pass criteria: the focused provider-readiness test file passes, and no broad
AWF/GitHub-owned validation suite is executed inside the agent phase.
