# PRRT_kwDOSJAM6s6DSvbY Plan

## Problem Statement and Scope

An unresolved PR review thread reports that protected workflow semantic allowlisting
classifies newly added informational/comment/notify steps as safe even when the new
step introduces a `uses:` action. That lets an unowned workflow edit run newly added
CI code while bypassing the existing `uses:` checks, which only compare matched
existing steps.

Scope is limited to the protected workflow quality-gate classifier and focused
regression coverage for added informational steps/jobs with `uses:`.

## Requirements Checklist

- Add a failing regression proving an unowned protected workflow edit cannot add an
  informational/comment/notify step that contains `uses:`.
- Tighten the informational-step classifier so added steps/jobs with `uses:` are not
  treated as informational.
- Preserve existing allowances for matched existing comment/notify steps and pinned
  `uses:` bumps.
- Keep violation reporting actionable with the existing added-step/job reasons.
- Validate with the narrow quality-gate test surface and static checks needed for
  this touched module.

## Implementation Steps

1. Add a unit test in `tests/unit/control/test_quality_gates.py` for an existing job
   where a newly added notify/comment step contains `uses: attacker/action@main`.
2. Run that test and confirm it fails before implementation.
3. Update `_is_informational_step` in `src/awf/control/quality_gates.py` to fail
   closed when the step defines `uses:`.
4. Run the new regression and the broader quality-gate unit tests.
5. Run `ruff` and `mypy` on the touched Python surface.
6. Record requirement-by-requirement validation in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  passes.
