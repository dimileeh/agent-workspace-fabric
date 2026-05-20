# Validation

Plan reference:
`plans/REVIEW_PRRT_kwDOSJAM6s6DjY48_PINNED_BUMP_INPUTS_PLAN.md`

# Requirement Status

- Add a regression test proving an allowed `python-version` bump is accepted
  when an existing sensitive input is unchanged: Complete.
- Keep additions, removals, and modifications of unapproved or sensitive inputs
  blocked: Complete.
- Keep unsafe GitHub Actions expressions in changed allowed input values
  blocked: Complete.
- Validate with the focused regression test and the quality-gates unit tests:
  Complete.

# Evidence

Files changed:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/REVIEW_PRRT_kwDOSJAM6s6DjY48_PINNED_BUMP_INPUTS_PLAN.md`
- `plans/REVIEW_PRRT_kwDOSJAM6s6DjY48_PINNED_BUMP_INPUTS_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_version_bump_allows_unchanged_sensitive_with_input -q`
  - First run before implementation failed with a protected workflow `with`
    violation.
  - Re-run after implementation passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q`
  - Passed: `311 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py`
  - Passed.

# Gaps

None.
