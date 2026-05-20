# PRRT_kwDOSJAM6s6DW69Q Prerelease Semver Validation

## Plan Reference

`plans/PRRT_kwDOSJAM6s6DW69Q_PRERELEASE_SEMVER_PLAN.md`

## Requirement Status

- Complete: Added a regression test showing an unowned protected workflow
  blocks pinned action ref changes from higher prerelease refs to lower
  prerelease refs.
- Complete: Replaced raw prerelease full-string ordering with parsed
  prerelease identifier ordering, including numeric identifier comparison.
- Complete: Existing pinned version upgrade coverage still passes in the
  quality-gates unit suite.
- Complete: Blocked prerelease downgrades report through the existing
  `workflow action changed outside pinned ref bump` violation path.

## Evidence

Changed files:

- `src/awf/control/quality_gates.py`
- `tests/unit/control/test_quality_gates.py`
- `plans/PRRT_kwDOSJAM6s6DW69Q_PRERELEASE_SEMVER_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DW69Q_PRERELEASE_SEMVER_VALIDATION.md`

Expected pre-fix failure confirmed:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_prerelease_downgrade_is_blocked -q
```

The new regression failed with zero violations before the implementation.

Post-fix commands run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py::test_workflow_pinned_uses_prerelease_downgrade_is_blocked -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_quality_gates.py -q
uv run --python 3.12 --extra dev ruff check src/awf/control/quality_gates.py tests/unit/control/test_quality_gates.py
uv run --python 3.12 --extra dev mypy src/awf/control/quality_gates.py
```

All post-fix commands passed.
