# Address Review Comment 4586615053 Plan

## Problem Statement and Scope

PR review comment `issue:4586615053` reports two residual Cursor runtime design
points:

- `infer_provider(model="sonnet-4-thinking", output=None)` can infer
  `anthropic` because the Cursor default model contains the generic Anthropic
  marker `sonnet`.
- Cursor custom non-thinking defaults intentionally bypass effort mapping, but
  the helper contract does not document that operator-facing tradeoff.

The scoped work is limited to:

- `src/awf/adapters/provider_failures.py`
- `src/awf/adapters/model_selection.py`
- focused adapter/provider-failure tests
- this plan and its validation artifact

## Requirements Checklist

- Add a regression test proving the Cursor default thinking model is inferred
  as `cursor`, even without Cursor-specific output text.
- Update provider inference so Cursor-specific model markers win before generic
  Anthropic markers.
- Document the `cursor_selected_model` precedence contract: explicit model
  first, then custom default, then effort mapping when the default is absent or
  already Cursor's thinking default.
- Add or preserve test coverage showing custom non-thinking Cursor defaults
  intentionally bypass effort mapping for high/xhigh efforts.
- Do not run broad AWF/GitHub-owned validation; use focused local checks only.

## Implementation Steps

1. Add focused failing tests for Cursor provider inference and custom-default
   effort bypass behavior.
2. Run the new provider-inference test before implementation and confirm the
   current Anthropic inference failure.
3. Add Cursor's default thinking model to the Cursor provider markers checked
   before Anthropic markers.
4. Expand the `cursor_selected_model` docstring to state the custom-default
   precedence tradeoff.
5. Run focused adapter/provider-failure tests and focused lint for changed
   files.
6. Create the validation artifact with requirement-by-requirement evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_provider_failures.py::test_cursor_default_thinking_model_infers_cursor_without_output -q`
  - Expected to fail after the test-only edit and before the inference fix.
  - Expected to pass after the inference fix.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestCursorAdapter::test_custom_default_model_bypasses_effort_mapping -q`
  - Expected to pass after the model-selection contract test/doc update.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters/provider_failures.py src/awf/adapters/model_selection.py tests/unit/adapters/test_provider_failures.py tests/unit/adapters/test_adapters.py`
  - Expected to pass.

Full AWF/GitHub validation is intentionally left to AWF after agent completion.
