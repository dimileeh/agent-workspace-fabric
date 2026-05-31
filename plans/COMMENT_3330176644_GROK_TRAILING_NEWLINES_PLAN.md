# Comment 3330176644 Grok Trailing Newlines Plan

## Problem Statement And Scope

The Grok adapter launcher reads AWF prompts with `prompt="$(cat)"`, which loses
all trailing newlines because POSIX command substitution strips them. This
creates a prompt-fidelity divergence from adapters that stream stdin bytes
directly.

Scope is limited to the Grok launcher and its focused adapter regression tests.

## Requirements Checklist

- Add a regression test that proves Grok launcher prompt delivery preserves
  trailing newlines.
- Update the launcher to preserve trailing newlines while still keeping prompt
  payloads out of argv before `grok` is executed.
- Keep Grok CLI flags and model selection behavior unchanged.
- Run only targeted adapter tests; full AWF/GitHub validation is managed by AWF
  after agent completion.

## Implementation Steps

1. Update the Grok launcher subprocess test to send a prompt ending in multiple
   newlines and expect the exact value in `grok -p`.
2. Run that focused test and confirm it fails against the current launcher.
3. Change `_grok_launcher_script` to avoid the trailing-newline stripping bug.
4. Update any string-level launcher contract assertions needed for the new
   implementation.
5. Re-run the focused Grok adapter tests.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter::test_launcher_reads_stdin_and_passes_prompt_to_official_single_flag -q`
  - First run should fail before implementation, then pass after the launcher
    change.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter -q`
  - Passes after implementation.
