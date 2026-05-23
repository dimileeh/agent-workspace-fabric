# PRRT_kwDOSJAM6s6ESXeu Plan

## Problem Statement And Scope

The PR review reports that `awf init <path>` no longer catches unexpected
failures from `preview_project_onboarding`, causing repository inspection errors
such as `OSError` to surface as raw tracebacks instead of a friendly CLI error.

Scope is limited to restoring the friendly error handling for project
onboarding preview construction and adding a focused regression test.

## Requirements Checklist

- [ ] Preserve `ValueError` handling as a usage error with exit code 2.
- [ ] Convert unexpected preview construction failures to a friendly error with
      exit code 1.
- [ ] Prevent raw tracebacks in the CLI output for this path.
- [ ] Add a focused regression test for an unexpected preview failure.
- [ ] Run only focused validation owned by this change; AWF/GitHub own broad
      validation after agent completion.

## Implementation Steps

1. Add a CLI unit test that stubs local prerequisites and makes
   `preview_project_onboarding` raise `OSError`.
2. Confirm the new regression fails against the current code.
3. Add the generic exception guard around preview construction in
   `src/awf/cli/main.py`.
4. Re-run the focused regression test and a narrow lint check on touched files.
5. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k unexpected_preview`
  - Passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally not run during the agent phase.
