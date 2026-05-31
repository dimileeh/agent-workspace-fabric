# Cursor Env Auth Review Fix Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F79IB` reports that Cursor readiness emits
`STATIC_TOKEN_FALLBACK` when `CURSOR_API_KEY` is present, even though
`CURSOR_API_KEY` is the expected Cursor CLI auth mechanism rather than a
fallback from file auth.

Scope is limited to Cursor provider readiness warning behavior and focused
unit coverage for that behavior.

## Requirements Checklist

- Preserve Cursor readiness success when `CURSOR_API_KEY` is configured.
- Stop emitting the misleading `STATIC_TOKEN_FALLBACK` warning for Cursor env auth.
- Keep static-token fallback warnings for providers where env auth is a fallback.
- Avoid exposing secret values in readiness payloads.
- Run only focused local checks; AWF/GitHub own broad validation after agent completion.

## Implementation Steps

1. Add/update focused regression assertions showing Cursor env auth has no warnings.
2. Confirm the focused regression fails against the current implementation.
3. Update Cursor readiness implementation to suppress the misleading warning.
4. Re-run focused provider readiness tests.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_cursor_env_present tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_provider_readiness_env_fallbacks_report_security_warnings -q`

Pass criteria: targeted tests pass after implementation. Full AWF/GitHub validation is intentionally left to the AWF post-agent pipeline.
