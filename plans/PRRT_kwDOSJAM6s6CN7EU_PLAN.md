# PRRT_kwDOSJAM6s6CN7EU Plan

## Problem Statement And Scope

The PR review thread reports that `AWF_CALLBACKS_ALLOWED_HOSTS` fails when set
as a normal comma-separated environment variable because `pydantic-settings`
JSON-decodes tuple fields before the existing `mode="before"` validator can
split the string. Scope is limited to `Settings.callbacks_allowed_hosts`
configuration parsing and focused regression coverage.

## Requirements Checklist

- Reproduce the environment-variable path with a failing unit test.
- Preserve existing constructor parsing for comma-separated strings, lists, and
  tuples.
- Allow `AWF_CALLBACKS_ALLOWED_HOSTS=operator.example.com,backup.example.com`
  to normalize to `("operator.example.com", "backup.example.com")`.
- Keep invalid non-string/list/tuple values rejected with the existing message.
- Avoid unrelated settings, callback delivery, or service behavior changes.

## Implementation Steps

1. Add a focused unit test for comma-separated `AWF_CALLBACKS_ALLOWED_HOSTS`.
2. Run the focused test to confirm the current failure.
3. Adjust the settings field so pydantic-settings passes raw env strings to the
   existing validator instead of JSON-decoding first.
4. Run focused settings tests and narrow lint/type checks for the touched area.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/common/test_common_polish.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/common/config.py tests/unit/common/test_common_polish.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf/common/config.py`
  passes, or any limitation is documented in validation.
