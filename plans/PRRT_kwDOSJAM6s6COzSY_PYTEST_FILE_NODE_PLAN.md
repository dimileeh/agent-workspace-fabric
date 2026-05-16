# PRRT_kwDOSJAM6s6COzSY Pytest File Node Plan

## Problem Statement And Scope

An unresolved review thread reports that pytest collection/import failures can
appear in short-summary output as file-level `ERROR tests/example.py - ...`
entries. The current parser records the evidence line but only extracts node
IDs that include at least one `::` component.

Scope is limited to pytest failure evidence parsing in
`src/awf/runtime/validation.py` and focused regression coverage in
`tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Capture file-level pytest node IDs for short-summary `ERROR` lines.
- Preserve existing class, function, and parametrized node ID behavior.
- Preserve the existing boundary that non-`ERROR` file-only summaries do not
  become node IDs.
- Keep fallback evidence collection unchanged.

## Implementation Steps

1. Add a failing parser regression for `ERROR tests/unit/test_imports.py - ...`.
2. Update the pytest node regex/parser to allow module-only nodes only where the
   summary kind permits them.
3. Re-run the targeted parser tests and the narrow runtime validation test file
   if practical.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- If only a narrower command is run, it must include the new regression and
  nearby existing parser tests.
