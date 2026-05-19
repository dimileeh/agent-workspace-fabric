# CI Lint Dependency Install Validation

Plan reference: `plans/CI_LINT_DEPENDENCY_INSTALL_PLAN.md`

## Requirement Status

- Add a focused regression test for the `lint-and-type` workflow install
  command: Complete.
  Evidence: `tests/unit/test_ci_workflow.py`.
- Ensure `lint-and-type` installs dev dependencies with `uv sync` and the
  checked-in `uv.lock`: Complete.
  Evidence: `.github/workflows/ci.yml` now uses
  `uv sync --python 3.12 --locked --extra dev`.
- Ensure the job no longer performs ad hoc `uv pip install -e ".[dev]"`
  dependency resolution: Complete.
  Evidence: the regression test asserts that command is absent.
- Run the focused workflow regression test and a direct local `uv sync`
  verification: Complete.
- Recheck PR #265 status so residual CI state is visible: Complete.

## Evidence

- Pre-fix focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow.py -q`
  failed because the workflow still contained
  `uv venv --python 3.12` plus `uv pip install -e ".[dev]"`.
- Post-fix focused regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow.py -q`
  passed, 1 test.
- New test lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/test_ci_workflow.py`
  passed.
- Updated install command:
  `uv sync --python 3.12 --locked --extra dev`
  passed.
- Replayed skipped `lint-and-type` checks locally after dependency install:
  `.venv/bin/ruff check .`
  passed.
- Replayed skipped format check:
  `.venv/bin/ruff format --check .`
  passed.
- Replayed skipped type check:
  `.venv/bin/mypy`
  passed.
- PR status recheck:
  `gh pr checks 265 --json name,state,bucket,link,startedAt,completedAt,workflow`
  still showed the pre-fix `lint-and-type` job failed on run `26127890417`
  while `console` and `release-artifacts` passed and `python-full-coverage`
  was still in progress. A new CI run requires AWF to push this local commit.

## Gaps

- The remote PR run has not exercised this local commit yet because AWF owns
  pushing. `python-full-coverage` was still in progress on the pre-fix run at
  the time of validation.
