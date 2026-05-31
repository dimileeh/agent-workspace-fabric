# Review PRRT_kwDOSJAM6s6F8TtL Cursor Failure Model Plan

## Problem Statement and Scope

Cursor lower-effort runs can intentionally omit `-m` even when the adapter
default model is `sonnet-4-thinking`. `AgentAdapter` failure logging and
provider-recovery metadata currently use `model or self._default_model`, which
can report the thinking model when the Cursor CLI did not select it.

Scope is limited to aligning adapter failure attribution with the selected CLI
model. No protected workflow or broad validation configuration files are in
scope.

## Requirements Checklist

- Add a regression test proving a lower-effort Cursor failure without an
  explicit model does not report `sonnet-4-thinking` in recovery metadata.
- Keep explicit Cursor model overrides attributed to the explicit model.
- Preserve existing non-Cursor default-model attribution behavior.
- Keep validation focused; AWF/GitHub own broad validation after agent
  completion.

## Implementation Steps

1. Add the focused Cursor failure regression test first and confirm it fails.
2. Add a small adapter hook for selected model attribution.
3. Override the hook in `CursorAdapter` using the existing Cursor model
   selection helper.
4. Update base adapter logging and provider-failure classification to use the
   selected model hook.
5. Run the targeted adapter test(s) that cover this behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py -q -k cursor`

Pass criteria: the targeted Cursor adapter tests pass, including the new
failure-metadata regression. Full AWF/GitHub validation is intentionally left to
AWF after agent completion.
