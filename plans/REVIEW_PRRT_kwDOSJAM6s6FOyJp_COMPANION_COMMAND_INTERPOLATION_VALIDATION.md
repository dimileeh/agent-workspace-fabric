# Companion Command Interpolation Review Fix Validation

Plan reference: `REVIEW_PRRT_kwDOSJAM6s6FOyJp_COMPANION_COMMAND_INTERPOLATION_PLAN.md`

## Requirement status

- Complete: Added a regression test proving companion `command` rejects Docker Compose interpolation syntax.
- Complete: Preserved existing accepted companion command behavior except for interpolation-bearing values.
- Complete: Reused the existing `_value_has_compose_interpolation` detector for command validation.
- Complete: Avoided broad AWF/GitHub-owned validation and used focused local checks only.

## Evidence

Files changed:

- `src/awf/api/schemas_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`
- `plans/REVIEW_PRRT_kwDOSJAM6s6FOyJp_COMPANION_COMMAND_INTERPOLATION_PLAN.md`
- `plans/REVIEW_PRRT_kwDOSJAM6s6FOyJp_COMPANION_COMMAND_INTERPOLATION_VALIDATION.md`

Focused checks run:

- Red check before implementation: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Result: failed on the new command interpolation case because no `ValidationError` was raised.
- Green check after implementation: `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_invalid_public_contract -q`
  - Result: `21 passed in 0.38s`.
- Targeted lint: `uv run --python 3.12 --extra dev ruff check src/awf/api/schemas_companions.py tests/unit/api/test_schema_coverage_edges.py`
  - Result: `All checks passed!`

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad validation, provenance, logs, timeouts, and merge gating after completion.
