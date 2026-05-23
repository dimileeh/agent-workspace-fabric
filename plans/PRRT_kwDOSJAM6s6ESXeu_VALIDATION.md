# PRRT_kwDOSJAM6s6ESXeu Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6ESXeu_PLAN.md`

## Requirement Status

- Preserve `ValueError` handling as a usage error with exit code 2:
  Complete. The existing `except ValueError` branch remains unchanged before
  the generic exception guard.
- Convert unexpected preview construction failures to a friendly error with
  exit code 1:
  Complete. `src/awf/cli/main.py` now catches unexpected exceptions from
  `preview_project_onboarding`, prints a concise onboarding-preview error, and
  exits with code 1.
- Prevent raw tracebacks in the CLI output for this path:
  Complete. The new regression asserts no `Traceback` appears when preview
  construction raises `OSError`.
- Add a focused regression test for an unexpected preview failure:
  Complete. `tests/unit/cli/test_init.py` covers an `OSError` from preview
  construction.
- Run only focused validation owned by this change:
  Complete. Full AWF/GitHub validation was not run during the agent phase and is
  managed by AWF after agent completion.

## Evidence

- Failing-before-fix command:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k unexpected_preview`
  - Failed because the CLI output was empty and the original `OSError`
    propagated.
- Passing-after-fix command:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k unexpected_preview`
  - Passed: `1 passed, 135 deselected`.
- Focused format command:
  `uv run --python 3.12 --extra dev ruff format tests/unit/cli/test_init.py`
  - Passed after reformatting the touched test file.
- Focused lint command:
  `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Passed: `All checks passed!`

## Remaining Gaps

None.
