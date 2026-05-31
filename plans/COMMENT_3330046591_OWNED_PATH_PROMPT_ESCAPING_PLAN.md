# Comment 3330046591 Owned Path Prompt Escaping Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F7mMV` reports that
`src/awf/runtime/monitor_prompts.py` embeds workspace `owned_paths` directly in
comment and CI repair prompts. Because `OwnedPath` is currently constrained only
by length, an entry containing a newline or prompt-control text can break out of
the declared-owned-paths list and appear as agent instructions.

Scope is limited to prompt rendering for PR-monitor repair prompts that include
declared `owned_paths`; this does not change stored schema validation.

## Requirements Checklist

- Render each declared `owned_paths` entry as quoted/escaped untrusted data in
  monitor prompts.
- Preserve the existing protected-file policy behavior and "owned protected
  paths are editable" guidance.
- Add regression coverage showing newline/control-text owned paths do not create
  standalone prompt instruction lines.
- Keep validation focused to the touched prompt unit tests and targeted lint for
  touched files.
- Record validation evidence in a matching validation document.

## Implementation Steps

1. Add failing tests in `tests/unit/runtime/test_monitor_prompts.py` for
   thread, review-comment, and CI prompt owned-path escaping.
2. Update `src/awf/runtime/monitor_prompts.py` to render declared owned paths as
   JSON-escaped string literals in the list.
3. Adjust existing owned-path display assertions to the new quoted form.
4. Run focused unit tests for `tests/unit/runtime/test_monitor_prompts.py`.
5. Run targeted ruff on the touched source and test files.
6. Create validation documentation and commit the focused fix.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/monitor_prompts.py tests/unit/runtime/test_monitor_prompts.py`
  passes.
- Full AWF/GitHub validation is intentionally not run during the agent phase;
  AWF owns broad validation, provenance, and merge gating after completion.
