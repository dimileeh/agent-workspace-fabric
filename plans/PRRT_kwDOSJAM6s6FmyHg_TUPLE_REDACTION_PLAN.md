# PRRT_kwDOSJAM6s6FmyHg Tuple Redaction Plan

## Problem Statement And Scope

The PR review reports that `redact_first_run_value()` preserves tuple containers
while redacting provider references, but then calls `redact_audit_value()`, whose
default behavior converts tuples to lists. The public first-run redaction helper
therefore does not preserve tuple container types for callers that do not go
through the private `_redact_provider_refs()` helper directly.

Scope is limited to first-run redaction and the common audit redaction option
needed to preserve tuples for that caller. Existing audit payload defaults must
remain JSON-oriented and continue converting tuples to lists unless explicitly
requested.

## Requirements Checklist

- Add a regression test that exercises `redact_first_run_value()` directly with
  tuple input containing provider refs and token-like values.
- Keep provider refs, sensitive keys, and token-like strings redacted after both
  first-run and audit redaction passes.
- Preserve tuple containers in the public first-run helper, including nested
  tuples.
- Preserve the existing default `redact_audit_value()` tuple-to-list behavior
  for audit payload callers.
- Run only focused local checks for the changed behavior; AWF/GitHub own broad
  validation after agent completion.

## Implementation Steps

1. Update `tests/unit/service/test_host_setup_rendering.py` with a failing public
   API regression test for `redact_first_run_value()`.
2. Add an opt-in tuple-preserving mode to `redact_audit_value()` while keeping
   the current default behavior unchanged.
3. Call the tuple-preserving audit mode from `redact_first_run_value()`.
4. Run the focused host setup rendering tests that cover the changed behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in the agent phase per the
  workspace contract.
