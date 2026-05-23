# PRRT_kwDOSJAM6s6ES_2h Guided Preview Exception Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6ES_2h_GUIDED_PREVIEW_EXCEPTION_PLAN.md`

## Requirement Status

- Complete: Preserve unsupported-template validation before preview rebuild.
  - Existing validation still rejects a template choice that is not in
    `supported_templates` before calling `preview_factory`.
- Complete: Preserve `ValueError` preview failures as usage errors with exit
  code 2.
  - Added parameterized coverage for `ValueError` from guided template-change
    preview rebuild.
- Complete: Convert unexpected preview rebuild failures to a friendly error
  with exit code 1.
  - Added the same generic exception guard used by the initial preview path.
- Complete: Prevent raw tracebacks in CLI output for the guided template-change
  path.
  - Regression test asserts no `Traceback` text is emitted for either failure
    class.
- Complete: Run only focused validation for the touched CLI behavior.
  - Full AWF/GitHub validation was not run during the agent phase; AWF/GitHub
    own broad validation after completion.

## Evidence

Files changed:

- `src/awf/cli/main.py`
- `tests/unit/cli/test_init.py`
- `plans/PRRT_kwDOSJAM6s6ES_2h_GUIDED_PREVIEW_EXCEPTION_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6ES_2h_GUIDED_PREVIEW_EXCEPTION_VALIDATION.md`

Focused checks:

- Before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k guided_template_change_preview_failure`
  - Result: failed because `ValueError` and `OSError` from `preview_factory`
    propagated out of `_prompt_project_onboarding_choices`.
- After implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k guided_template_change_preview_failure`
  - Result: passed, `2 passed, 136 deselected`.
  - `uv run --python 3.12 --extra dev ruff format tests/unit/cli/test_init.py`
  - Result: passed, reformatted one touched test file.
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k guided_template_change_preview_failure`
  - Result: passed, `2 passed, 136 deselected`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Result: passed.

## Remaining Gaps

None.
