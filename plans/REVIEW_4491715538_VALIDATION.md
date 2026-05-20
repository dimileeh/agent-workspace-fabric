# Review 4491715538 Validation

Plan reference: `plans/REVIEW_4491715538_PLAN.md`

## Requirement Status

- Confirm `_step_remainder` continues to ignore `id` and `name` so cosmetic
  renames on matched steps do not produce remainder violations: Complete.
  `_step_remainder` already ignored those fields in the current checkout, and
  `test_workflow_step_name_change_is_allowed_when_step_id_matches` now locks
  that behavior.
- Add a regression for validation command prefix boundaries such as
  `uv run pytest` versus `uv run pytest-cov report`: Complete. The parameterized
  validation preservation test now covers both `pytest&& ruff check` and
  `uv run pytest-cov report`; the former failed before implementation and
  passes after the boundary guard.
- Add a regression proving first-time `[dependency-groups]` additions are
  allowed when the new group entries are supported string lists: Complete.
  `test_pyproject_first_dependency_groups_section_is_allowed` failed before
  implementation and passes after allowing new groups only when the old section
  is absent.
- Preserve the existing policy that adding a new dependency group to an
  existing `[dependency-groups]` section is blocked: Complete.
  `test_pyproject_new_dependency_group_is_blocked` still passes.
- Keep changes scoped to the classifier, focused tests, and required plan and
  validation artifacts: Complete.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_PLAN.md`
- `plans/REVIEW_4491715538_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_pyproject_first_dependency_groups_section_is_allowed tests/unit/control/test_quality_gates.py::test_validation_run_preservation_allows_only_safe_validation_appends -q`
  - Failed before implementation on the first-time dependency group addition and
    the `pytest&& ruff check` prefix-boundary case.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_step_name_change_is_allowed_when_step_id_matches tests/unit/control/test_quality_gates.py::test_pyproject_first_dependency_groups_section_is_allowed tests/unit/control/test_quality_gates.py::test_pyproject_new_dependency_group_is_blocked tests/unit/control/test_quality_gates.py::test_validation_run_preservation_allows_only_safe_validation_appends -q`
  - Passed: 28 passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passed: 316 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Passed: All checks passed.

## Gaps

No remaining gaps. The `_step_remainder` item was already fixed in this
checkout; this iteration added a regression test for it and fixed the two
remaining actionable classifier issues.
