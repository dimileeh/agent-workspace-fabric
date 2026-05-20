# Review 4491715538 Unchanged Project Dependencies Validation

Plan reference: `REVIEW_4491715538_UNCHANGED_PROJECT_DEPENDENCIES_PLAN.md`

## Requirement Status

- Add a regression test proving unchanged unsupported `project.dependencies`
  does not emit a protected-file violation when an unrelated allowed project
  metadata edit changes the file: Complete.
- Preserve fail-closed behavior for changed unsupported dependency lists:
  Complete.
- Keep the fix scoped to the dependency-list classifier: Complete.
- Validate with the narrow quality-gate test surface: Complete.
- Commit the local fix without pushing or switching branches: Complete.

## Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_4491715538_UNCHANGED_PROJECT_DEPENDENCIES_PLAN.md`
- `plans/REVIEW_4491715538_UNCHANGED_PROJECT_DEPENDENCIES_VALIDATION.md`

TDD failure before implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "unchanged_unsupported_project_dependencies"`
- Result: failed with a `project.dependencies` unsupported-format violation.

Verification after implementation:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "unchanged_unsupported_project_dependencies or unsupported_dependency_shapes"`
- Result: 9 passed, 212 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
- Result: 221 passed.
