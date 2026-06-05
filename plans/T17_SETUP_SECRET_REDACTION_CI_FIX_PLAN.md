# T17 Setup Secret Redaction CI Fix Plan

## Problem Statement And Scope

PR #391 fails CI in `python-coverage-shards (7)` because
`test_service_logs_follow_flushes_multiline_secret_prefix_at_eof` renders
`stdout <redacted>` instead of preserving ordinary output that only matches the
first line of a configured multiline secret at EOF.

Scope is limited to service-log redaction context for Compose env files and the
focused tests that prove the CI failure is fixed.

## Requirements Checklist

- Reproduce the failing CI test locally before changing code.
- Preserve exact redaction of full multiline Compose secret values in captured
  and followed service logs.
- Do not treat the first physical line of an unclosed quoted multiline Compose
  env assignment as a standalone exact secret.
- Keep the fix minimal and avoid broad validation during the agent phase.
- Commit the local repair with a conventional CI-fix message.

## Implementation Steps

1. Inspect the service-log secret collection path and the focused failing tests.
2. Adjust Compose env secret collection so multiline quoted secret assignments
   contribute the full parsed multiline value without also contributing the
   parser's partial first-line value.
3. Re-run the failing test and adjacent service-log redaction tests.
4. Record validation evidence in
   `plans/T17_SETUP_SECRET_REDACTION_CI_FIX_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py::test_service_logs_follow_flushes_multiline_secret_prefix_at_eof -q`
  - Passes and renders the incomplete prefix unchanged.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs_parts/test_logs_part_002.py -q`
  - Passes the nearby service-log behavior surface.

Full AWF/GitHub validation remains managed by AWF after agent completion.
