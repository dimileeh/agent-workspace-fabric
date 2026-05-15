# PRRT_kwDOSJAM6s6CNPD Setup Dependency Evidence Plan

## Problem Statement and Scope

The setup dependency network classifier currently treats generic dependency words
in output from an unrecognized, non-compound setup command as sufficient
dependency context. This can misclassify a script failure such as
`./bootstrap` reporting `failed to fetch config: connection timed out` as a
setup dependency network failure.

Scope is limited to the setup dependency network classifier and its unit tests.

## Requirements Checklist

- Add a regression test for a standalone unrecognized setup script that reports
  a generic fetch timeout without package, index, or known dependency-host
  evidence.
- Preserve retry classification for recognized dependency tools such as `pip`,
  `npm`, `poetry`, `bundle`, `go install`, `gradle`, and `mvn` install-style
  commands.
- Preserve retry classification for unrecognized scripts when output includes
  specific package/index/known-host dependency evidence.
- Keep deterministic failures and existing transient category behavior intact.

## Implementation Steps

1. Add the failing regression test in `tests/unit/runtime/test_validation.py`.
2. Tighten the unrecognized-command fallback in
   `src/awf/runtime/validation.py` to require specific dependency evidence.
3. Run the narrow validation tests for the setup dependency classifier.
4. Run lint for touched Python files if time permits.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
