# Setup Dependency Classifier Review Plan

## Problem Statement And Scope

Address PR review comment `issue:4454303511` against the setup dependency
network classifier in `src/awf/runtime/validation.py`.

The scope is limited to classifier false positives:

- A URL or registry endpoint using port `401` or `403` must not be treated as a
  deterministic HTTP authorization failure by bare-number regex matching.
- The context fallback must not treat an unrelated setup script as dependency
  setup solely because its output contains the generic word `simple`.

## Requirements Checklist

- Add regression coverage before implementation for the non-standard port case.
- Add regression coverage before implementation for unrelated script output that
  contains `simple` plus a transient DNS phrase.
- Preserve existing deterministic handling for recognizable HTTP 401/403
  status contexts.
- Preserve existing transient retry classification for real dependency setup
  commands and PyPI `/simple/` output.
- Keep changes scoped to validation classifier behavior and its unit tests.

## Implementation Steps

1. Add failing unit tests in `tests/unit/runtime/test_validation.py` for the two
   review-reported false positives.
2. Run the focused tests to confirm the current implementation fails.
3. Tighten `_SETUP_DETERMINISTIC_FAILURE_RE` so raw `401`/`403` numbers only
   count in recognizable HTTP status contexts.
4. Tighten dependency-context fallback so `simple` only counts when it appears
   as a package-index path segment, while the other dependency terms keep their
   current behavior.
5. Run focused classifier tests, then the unit test file or narrower command
   that proves the touched behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  should pass.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  should pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  should pass if runtime allows the broader typecheck.
