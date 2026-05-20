# Review 4491715538 Tool Diagnostics Validation

Plan reference:
`plans/REVIEW_4491715538_TOOL_DIAGNOSTICS_PLAN.md`

## Requirement Status

- Complete: Preserved the existing behavior that unowned coverage
  `fail_under` changes are blocked for both lower and higher values.
- Complete: Clarified the raised `fail_under` reason so it states that the
  protected coverage policy change requires ownership of `pyproject.toml`.
- Complete: Added
  `test_pyproject_reports_all_unknown_tool_section_changes` to prove multiple
  changed non-policy `tool.*` sections are all surfaced.
- Complete: Kept production changes focused in
  `src/awf/control/quality_gates.py`.
- Complete: Ran focused and broader quality-gate validation commands.

## Evidence

- Failing-before evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "raising_coverage_fail_under or unknown_tool"`
  failed with the missing ownership phrase and only `tool.black` being reported.
- Passing focused evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q -k "raising_coverage_fail_under or unknown_tool"`
  passed with `2 passed, 100 deselected`.
- Passing quality-gates unit evidence:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  passed with `102 passed`.
- Lint evidence:
  `uv run --python 3.12 --extra dev ruff check src/awf tests` passed.
- Type evidence:
  `uv run --python 3.12 --extra dev mypy src/awf` passed.
- Broad unit-suite note:
  `uv run --python 3.12 --extra dev pytest tests/unit -q` was attempted but
  stopped after several minutes at 7% progress because the planned focused
  quality-gate validation had already completed.
