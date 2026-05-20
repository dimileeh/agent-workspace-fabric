# PRRT_kwDOSJAM6s6DWl-e PEP 735 Unchanged Group Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6DWl-e_PEP735_UNCHANGED_GROUP_PLAN.md`

## Requirement Status

- Complete: Added
  `test_pyproject_unchanged_pep735_include_group_is_not_revalidated` to cover an
  unchanged PEP 735 `{ include-group = "test" }` entry with an unrelated runtime
  dependency addition.
- Complete: Preserved existing dependency-change protections by changing only
  the existing-group comparison path and leaving new/changed unsupported groups
  on the existing validation path.
- Complete: Made the production change in
  `src/awf/control/quality_gates.py` by skipping dependency-list validation for
  groups whose parsed value is unchanged.
- Complete: Ran focused and broader validation commands.

## Evidence

- Failing-before evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "unchanged_pep735"`
  failed with `dependency section has unsupported format: dependency-groups.dev`.
- Passing focused evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "pep735 or dependency_group"`
  passed with `2 passed, 99 deselected`.
- Lint evidence:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  passed.
- Broader unit evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with `101 passed`.
