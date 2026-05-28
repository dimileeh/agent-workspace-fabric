# PR295 Python Full Coverage Plan

## Problem Statement And Scope

PR #295 fails the `python-full-coverage` CI job because the test suite passes but total
coverage is 98.90%, below the required 99.00% threshold. The dependent
`ci-required` check fails only because `python-full-coverage` fails.

GitHub Actions coverage output identifies the current PR's host setup files as the
main uncovered surface:

- `src/awf/host_setup/config.py`
- `src/awf/host_setup/source_assets.py`

This fix is scoped to adding focused unit coverage for those host setup branches. It
must not weaken CI, edit protected workflow/config files, switch branches, push, or
run AWF/GitHub-owned broad validation locally.

## Requirements Checklist

- Add tests for the uncovered `host_setup.config` branches reported by CI.
- Add tests for the uncovered `host_setup.source_assets` branches reported by CI.
- Preserve reason-coded, secret-free diagnostics behavior.
- Keep changes limited to source-adjacent tests and mandatory plan/validation docs.
- Run only focused local validation; leave full coverage and CI-equivalent validation
  to AWF/GitHub after agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Reproduce the coverage failure with a narrow command for
   `tests/unit/service/test_host_setup_config.py` against `awf.host_setup`.
2. Add focused tests to `tests/unit/service/test_host_setup_config.py` covering:
   - config default/empty/corrupt/secret-validation paths;
   - safe handling of recursive mappings and non-string YAML keys;
   - fallback paths for config path normalization and non-POSIX chmod behavior;
   - source checkout unreadable markers, stale metadata contracts, unreadable roots,
     non-POSIX readability, and clock fallback.
3. Re-run the focused host setup test module.
4. Re-run the focused host setup package coverage command.
5. Record the evidence in `plans/PR295_PYTHON_FULL_COVERAGE_VALIDATION.md`.
6. Commit the local changes without pushing.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py --cov=awf.host_setup --cov-report=term-missing --cov-fail-under=99 -q`
  - Expected before fix: fails because focused host setup coverage is below 99%.
  - Expected after fix: passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q`
  - Expected after fix: passes.

Full repository coverage and required CI checks are intentionally not run locally in
this AWF agent phase; AWF/GitHub own broad validation, provenance, logs, timeouts,
and merge gating after completion.
