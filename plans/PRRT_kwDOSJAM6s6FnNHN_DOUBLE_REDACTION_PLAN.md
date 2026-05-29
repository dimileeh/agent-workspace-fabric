# PRRT_kwDOSJAM6s6FnNHN Double Redaction Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6FnNHN` reports that first-run provider reference
redaction garbles assignment-shaped strings. `_redact_provider_refs()` replaces
`env://`, `keyring://`, and `plain-file://` references with `[redacted]`, then
immediately runs audit text redaction over the substituted string. For
assignment-shaped content such as `TOKEN=env://MY_KEY`, the audit assignment
pattern stops before the closing `]`, producing `TOKEN=[redacted]]`.

Scope is limited to `src/awf/host_setup/rendering.py`, focused host setup
rendering tests, and this plan/validation evidence.

## Requirements Checklist

- Add a regression proving assignment-shaped provider refs render as exactly
  `TOKEN=[redacted]` with no trailing bracket.
- Preserve provider-ref redaction for nested first-run values.
- Preserve delegated audit redaction of token-shaped strings after provider-ref
  redaction.
- Keep local validation focused; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Add a focused regression in `tests/unit/service/test_host_setup_rendering.py`
   for `redact_first_run_value("TOKEN=env://MY_KEY")`.
2. Run that single test before implementation and record the expected failure.
3. Update `_redact_provider_refs()` so string handling only redacts provider
   references; `redact_first_run_value()` will continue to invoke
   `redact_audit_value()` as the separate token audit pass.
4. Re-run the new regression and the focused host setup rendering test file.
5. Create the validation artifact with requirement status and focused command
   evidence.

## Assumptions/Changes

- The pre-fix regression produced `TOKEN=[redacted]]]`, showing the marker is
  reprocessed by both the local provider-ref text redactor and the final audit
  value redactor. To avoid marker reprocessing entirely, the implementation will
  run audit redaction on raw values first, then apply provider-ref redaction as
  the final first-run-specific pass.

## Verification Commands And Pass Criteria

- Pre-fix regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py::test_first_run_redaction_does_not_double_redact_provider_ref_assignments -q`
  - Expected before implementation: fails with `TOKEN=[redacted]]`.
- Post-fix focused regression:
  same command passes.
- Focused rendering surface:
  `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q`
  - Expected after implementation: passes.
