# PRRT_kwDOSJAM6s6DWkcy Informational Step Keys Plan

## Problem Statement and Scope

The protected workflow guardrail allows added informational/comment/notify steps when
their label or action and `run` command look safe. It does not currently reject
step-level execution fields such as `shell`, which can run arbitrary commands before
an otherwise safe `run: echo ok`.

Scope is limited to added informational workflow steps and informational jobs in
`src/awf/control/quality_gates.py`.

## Requirements Checklist

- Add regression coverage showing an added informational step with custom `shell`
  is rejected.
- Add regression coverage showing an added informational job containing such a
  step is rejected.
- Enforce a narrow step-key allowlist for informational steps before accepting
  them as safe.
- Preserve existing allowed informational echo/comment behavior.

## Implementation Steps

1. Add failing tests in `tests/unit/control/test_quality_gates.py` for the
   shell-field bypass on added steps and added informational jobs.
2. Run the focused tests and confirm the new regression fails before the fix.
3. Add an informational step allowed-key constant and reject steps containing
   keys outside that set.
4. Run focused quality gate tests, then targeted lint/type checks if practical.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes if runtime dependencies are available.
