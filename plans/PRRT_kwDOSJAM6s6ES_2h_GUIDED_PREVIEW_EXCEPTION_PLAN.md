# PRRT_kwDOSJAM6s6ES_2h Guided Preview Exception Plan

## Problem Statement And Scope

The PR review reports that guided `awf init <path>` catches unexpected preview
construction failures for the initial preview, but not when the user changes
the template and the guided prompt rebuilds the preview. That can expose raw
tracebacks for repository inspection errors such as unreadable project files.

Scope is limited to the guided template-change preview rebuild in
`src/awf/cli/main.py` and a focused regression test in
`tests/unit/cli/test_init.py`.

## Requirements Checklist

- [ ] Preserve unsupported-template validation before preview rebuild.
- [ ] Preserve `ValueError` preview failures as usage errors with exit code 2.
- [ ] Convert unexpected preview rebuild failures to a friendly error with exit
      code 1.
- [ ] Prevent raw tracebacks in CLI output for the guided template-change path.
- [ ] Run only focused validation for the touched CLI behavior; AWF/GitHub own
      broad validation after agent completion.

## Implementation Steps

1. Add a failing focused unit test for guided template change when
   `preview_factory` raises `OSError`.
2. Confirm the new regression fails against the current code.
3. Add matching `ValueError` and generic exception guards around the guided
   preview rebuild.
4. Re-run the focused regression test and a narrow lint check on touched files.
5. Record validation evidence in a matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k guided_template_change_preview_failure`
  - Fails before the fix and passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Passes after implementation.

Full AWF/GitHub validation is intentionally not run during the agent phase.
