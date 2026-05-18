# PRRT_kwDOSJAM6s6C-S1E Validation Handoff Plan

## Problem Statement And Scope

The conformance validation handoff classifier currently accepts a named validation
command handoff before running deterministic implementation-gap filters. A gap
that asks AWF to rerun validation commands while also asking the agent to
implement or wire an API/endpoint can therefore be treated as AWF-owned
validation evidence and stop the conformance loop early.

Scope is limited to the conformance validation-evidence classifier and focused
regression coverage for mixed named-command and implementation gaps.

## Requirements Checklist

- Named validation command handoff gaps remain accepted when they only ask AWF to
  run missing validation evidence.
- Mixed gaps that mention named validation commands and deterministic work such as
  implementing, wiring, API, or endpoint work remain agent-owned.
- Existing deterministic filters for migration, schema, documentation, code, and
  test work keep their current behavior.
- Add a regression test before changing implementation.

## Implementation Steps

1. Add a failing unit test covering named validation command handoff text mixed
   with deterministic API/endpoint implementation work.
2. Run the targeted test and confirm it fails on the current implementation.
3. Move the named handoff acceptance until after deterministic gap filters, while
   preserving the existing positive named-command handoff cases.
4. Run targeted and relevant broader validation commands.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_planning.py -q`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor.py -q`
  passes if runtime changes could affect executor handoff behavior.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/planning.py tests/unit/runtime/test_planning.py`
  passes.
