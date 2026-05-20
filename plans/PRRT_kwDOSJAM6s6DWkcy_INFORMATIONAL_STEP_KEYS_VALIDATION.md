# PRRT_kwDOSJAM6s6DWkcy Informational Step Keys Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DWkcy_INFORMATIONAL_STEP_KEYS_PLAN.md`

## Requirement Status

- Complete: Added regression coverage showing an added informational step with
  custom `shell` is rejected.
- Complete: Added regression coverage showing an added informational job
  containing such a step is rejected.
- Complete: Enforced a narrow step-key allowlist for informational steps before
  accepting them as safe.
- Complete: Preserved existing allowed informational echo/comment behavior in
  the full quality gate unit file.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DWkcy_INFORMATIONAL_STEP_KEYS_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DWkcy_INFORMATIONAL_STEP_KEYS_VALIDATION.md`

Commands run:

- Failed before fix as expected:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k custom_shell`
- Passed after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k custom_shell`
- Passed:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
- Passed:
  `uv run --python 3.12 --extra dev ruff check src/awf tests`
- Passed:
  `uv run --python 3.12 --extra dev mypy src/awf`

Optional broader unit command:

- `uv run --python 3.12 --extra dev pytest tests/unit -q` was stopped at 6%
  because it was progressing slowly and was broader than the narrow review fix.
  No failures were reported before termination.

## Gaps

No planned requirements remain partial or missing.
