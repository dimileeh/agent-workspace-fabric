# PRRT_kwDOSJAM6s6FLMwh Companion Git Branch Name Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6FLMwh_COMPANION_GIT_BRANCH_NAME_PLAN.md`

## Requirement Status

- Reject companion request names that cannot be used as the final Git branch
  path component: Complete.
- Preserve existing valid service names such as `backend`: Complete.
- Keep the fix in the companion request validation path so provisioning is not
  reached for invalid names: Complete.
- Add a regression test for the reported invalid names: Complete.
- Run focused schema tests only; full AWF/GitHub validation is managed after
  agent completion: Complete.

## Evidence

Changed files:

- `src/awf/common/companions.py`
- `src/awf/api/schemas_companions.py`
- `tests/unit/common/test_companions.py`
- `tests/unit/api/test_schema_coverage_edges.py`

Commands:

- Before implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_git_invalid_names -q`
  failed because `foo.lock`, `foo..bar`, and `.` did not raise
  `ValidationError`.
- After implementation,
  `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py::test_workspace_companions_reject_git_invalid_names -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_companions.py::test_companion_name_is_git_branch_component -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_schema_coverage_edges.py -q`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_companions.py -q`
  passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/companions.py src/awf/api/schemas_companions.py tests/unit/common/test_companions.py tests/unit/api/test_schema_coverage_edges.py`
  passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/common/companions.py src/awf/api/schemas_companions.py tests/unit/common/test_companions.py tests/unit/api/test_schema_coverage_edges.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/common/companions.py src/awf/api/schemas_companions.py`
  passed.

Full AWF/GitHub validation is intentionally left to the post-agent pipeline per
the workspace contract.
