# PRRT_kwDOSJAM6s6F786O Grok Runtime Reason Plan

## Problem Statement And Scope

The Grok launch preflight can return `GROK_RUNTIME_CLI_NOT_FOUND` when the
agent-runtime image lacks the `grok` executable, but the doctor reason text
catalog does not define operator-facing guidance for that code. This review
thread is scoped to adding the missing reason guidance and keeping the checked-in
reason catalog synchronized.

## Requirements Checklist

- Add a focused regression proving `GROK_RUNTIME_CLI_NOT_FOUND` has doctor
  guidance.
- Add `_ReasonText` for `GROK_RUNTIME_CLI_NOT_FOUND` with message, action,
  likely cause, related command, and docs link.
- Update `docs/REASON_CATALOG.md` so the catalog sync test remains true.
- Run only focused local checks; broad AWF/GitHub validation remains owned by
  AWF after agent completion.

## Implementation Steps

1. Add the failing regression in `tests/unit/service/test_doctor_reasons.py`.
2. Run the targeted regression to confirm it fails for the missing mapping.
3. Add the reason mapping in `src/awf/service/doctor/reasons.py`.
4. Update the generated catalog section in `docs/REASON_CATALOG.md`.
5. Re-run focused doctor reason tests and record the result in validation.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py -q`
  must pass.
- Full repository validation, coverage gates, frontend builds, and CI-equivalent
  checks are intentionally left to AWF/GitHub after this agent phase.
