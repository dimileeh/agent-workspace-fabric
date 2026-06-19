# PR614 Shard 8 Git Manager Line Limit Validation

Plan reference: `plans/PR614_SHARD8_GIT_MANAGER_LINE_LIMIT_PLAN.md`

## Requirement Status

- Complete: Preserved the behavior assertions by moving the git-manager
  ownership/cleanup edge tests into
  `tests/unit/node/test_git_manager_ownership_edges.py`.
- Complete: Did not edit CI, workflow, quality-gate, or threshold
  configuration.
- Complete: Kept changes limited to affected tests and plan/validation docs.
- Complete: Ran focused pytest and ruff checks manually; the repository
  pre-commit hook also ran its configured commit checks during `git commit`.
- Complete: Full AWF/GitHub validation remains owned by AWF/GitHub after agent
  completion.

## Evidence

- Failing focused repro before fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  failed with `tests/unit/node/test_git_manager.py: 1512`.
- Passing focused pytest after fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_git_manager_ownership_edges.py tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  passed: `3 passed in 0.45s`.
- Passing lint after fix:
  `uv run --python 3.12 --extra dev ruff check tests/unit/node/test_git_manager.py tests/unit/node/test_git_manager_ownership_edges.py plans/PR614_SHARD8_GIT_MANAGER_LINE_LIMIT_PLAN.md`
  passed: `All checks passed!`.
- Commit hook:
  `git commit -m "fix(ci): shard 8 line limit - split git manager edge tests"`
  passed its configured checks, including ruff, ruff format, and mypy.

## Residual Risk

The current CI run had remaining shards still completing while this fix was
prepared. This agent did not run local full coverage or CI-equivalent
validation; AWF/GitHub own those gates after completion.
