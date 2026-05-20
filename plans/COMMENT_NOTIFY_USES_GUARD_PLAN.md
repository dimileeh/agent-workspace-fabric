# Comment/Notify Uses Guard Plan

## Problem Statement And Scope

PR thread `PRRT_kwDOSJAM6s6DVGYw` reports that added workflow steps/jobs can be
classified as informational when their `uses:` action merely contains
`comment` or `notify`, such as `attacker/notify@main`. The change is scoped to
the workflow quality-gate classifier and focused unit coverage.

## Requirements Checklist

- Block newly added informational workflow steps that use untrusted
  comment/notify-looking actions.
- Block newly added informational workflow jobs that use untrusted
  comment/notify-looking actions in their steps.
- Preserve the existing allowance for known pinned PR comment actions.
- Keep protected workflow validation behavior otherwise unchanged.

## Implementation Steps

1. Add regression tests for `attacker/notify@main` as an added step and inside
   an added informational job.
2. Add coverage that the existing known PR comment action remains allowed.
3. Replace substring-only `uses:` classification with a narrow allowlist plus
   existing pinned-ref validation.
4. Run the focused unit tests, then lint the touched Python files if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
