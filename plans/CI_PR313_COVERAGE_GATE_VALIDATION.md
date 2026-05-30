# CI PR313 Coverage Gate Validation

Plan reference: `plans/CI_PR313_COVERAGE_GATE_PLAN.md`

## Requirement Status

- Complete: Do not edit protected workflow, quality-gate, or configuration files.
  Only a focused unit test module and plan/validation docs were changed.
- Complete: Do not run broad AWF/GitHub-owned validation locally. Local checks
  were limited to `tests/unit/common/test_owned_paths.py` and
  `awf.common.owned_paths` coverage reporting.
- Complete: Add targeted tests for real behavior in changed Python code. Added
  tests for fixed planning templates, non-mapping profile shapes, empty
  configured artifact entries, configured wildcard artifact entries, and static
  workspace-id glob pattern handling.
- Complete: Keep the fix scoped to the coverage failure. No production logic or
  CI gate configuration was changed.
- Complete: Commit the local fix with a conventional commit message. Commit is
  prepared after this validation file is written.

## Evidence

- GitHub Actions log inspection showed `python-full-coverage` passed 8,640 tests
  but failed with total coverage 98.99% against a 99% gate; `ci-required` failed
  only because that required job failed.
- Pre-fix focused repro:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py --cov=awf.common.owned_paths --cov-report=term-missing -q`
  failed locally because the configured 99% fail-under applied to the focused
  coverage run, and reported uncovered `awf.common.owned_paths` lines
  `80, 129, 150, 166` among the helper-module gaps.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py -q`
  passed with `54 passed in 0.50s`.
- Post-fix:
  `uv run --python 3.12 --extra dev ruff check tests/unit/common/test_owned_paths.py`
  passed with `All checks passed!`.
- Post-fix:
  `uv run --python 3.12 --extra dev pytest tests/unit/common/test_owned_paths.py --cov=awf.common.owned_paths --cov-report=term-missing --cov-fail-under=0 -q`
  passed with `54 passed in 0.61s` and reported
  `src/awf/common/owned_paths.py` at `100.00%`.

## Remaining Gaps

None for the focused fix. Full AWF/GitHub validation and the full coverage gate
are intentionally left to AWF after agent completion per the workspace
contract.
