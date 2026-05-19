# CI Lint Dependency Install Plan

## Problem Statement And Scope

PR #265's `lint-and-type` GitHub Actions job fails before linting or type
checking runs. The failing step uses `uv pip install -e ".[dev]"`, which
re-resolves editable development dependencies from PyPI and failed while
fetching `pygments-2.20.0` metadata with a broken-pipe connection error.

Scope is limited to the CI lint/type dependency setup. The fix should preserve
the existing lint, format, and mypy checks while making dependency installation
use the checked-in lockfile, matching the already-successful
`python-full-coverage` setup path.

## Requirements Checklist

- [ ] Add a focused regression test for the `lint-and-type` workflow install
  command.
- [ ] Ensure `lint-and-type` installs dev dependencies with `uv sync` and the
  checked-in `uv.lock`.
- [ ] Ensure the job no longer performs ad hoc `uv pip install -e ".[dev]"`
  dependency resolution.
- [ ] Run the focused workflow regression test and a direct local `uv sync`
  verification.
- [ ] Recheck PR #265 status so any remaining CI failures are visible.

## Implementation Steps

1. Add a unit test that parses `.github/workflows/ci.yml` and asserts the
   `lint-and-type` install step uses `uv sync --python 3.12 --locked --extra dev`
   without the previous editable `uv pip install` command.
2. Confirm that the new regression fails before the workflow change.
3. Update `.github/workflows/ci.yml` for the `lint-and-type` install step.
4. Run the new focused test, lint the new test file, and run the updated
   install command locally.
5. Record validation evidence in
   `plans/CI_LINT_DEPENDENCY_INSTALL_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow.py -q`
  fails before the workflow change and passes after it.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_ci_workflow.py`
  passes.
- `uv sync --python 3.12 --locked --extra dev`
  passes locally.
- `gh pr checks 265 --json name,state,bucket,link,startedAt,completedAt,workflow`
  is rechecked after the local fix to identify residual CI state.
