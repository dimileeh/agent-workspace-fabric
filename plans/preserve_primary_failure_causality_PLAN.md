# Preserve Primary Failure Causality Plan

## Problem Statement

AWF can overwrite an existing actionable workspace failure with a later stale,
cleanup, reconnect, lease-expiry, or terminal runtime release fault. The saved
AWF contract in `docs/awf-plans/ws_7038898eac3747ecaa53fb2c.md` is the source
of truth for this implementation.

## Scope

- Preserve existing primary failure fields and validation provenance when later
  recovery or cleanup paths encounter secondary infrastructure faults.
- Record stale, cleanup, reconnect, and terminal-runtime-release faults as
  secondary diagnostics.
- Keep no-primary behavior unchanged so stale and cleanup faults still classify
  as the primary failure when they are the first durable evidence.
- Reuse existing reason codes unless tests prove a new public code is required.

## Requirements Checklist

- Validation failure followed by stale-active handling keeps validation as the
  primary failure and records stale diagnostics secondarily.
- Validation failure followed by destroy cleanup failure keeps validation as the
  primary failure and records cleanup diagnostics secondarily.
- Validation failure followed by active-execution restart preservation keeps
  failure fields and provenance intact and emits primary context.
- Terminal-runtime-release cleanup failure does not replace validation
  provenance and emits primary context.
- Workspaces without primary evidence keep existing stale/cleanup primary
  classifications.
- Provider/auth/timeout primary failures are not collapsed to generic
  infrastructure failures by runtime stranding.
- Readiness taxonomy continues to treat preserved validation failures as
  classified/actionable.

## Implementation Steps

1. Add regression tests first in the focused worker, controls, and readiness
   suites.
2. Add narrow helpers for primary failure snapshots and secondary diagnostic
   payload shaping.
3. Update worker stale-active, stranded runtime, active preservation, and
   terminal release failure event paths.
4. Update destroy cleanup failure handling to preserve existing primary
   evidence while retaining cleanup operation/audit diagnostics.
5. Run focused unit tests plus ruff and mypy.
6. Write `plans/preserve_primary_failure_causality_VALIDATION.md` with
   requirement-by-requirement status and validation evidence.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py tests/unit/service/test_readiness.py tests/unit/api/test_validation_provenance.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_controls.py tests/unit/service/test_controls_lifecycle.py -q
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
```

Pass criteria: focused tests pass, static checks pass, and any remaining gap is
explicitly documented in the validation artifact.
