# CI PR 396 Monitor GC Line Limit Plan

## Problem Statement and Scope

PR #396 fails the Python full coverage job because the focused maintainability
guard `test_first_party_code_files_stay_under_line_limit` reports
`tests/unit/runtime/test_monitor_completion_gc.py` at 1,641 lines, above the
1,500-line first-party file limit.

Scope is limited to decomposing that oversized test module without changing
runtime behavior, disabling checks, or weakening assertions.

## Requirements Checklist

- Reproduce the reported focused failure before editing.
- Keep the maintainability guard unchanged.
- Split a coherent subset of `test_monitor_completion_gc.py` into a separate
  unit test module so all first-party code files are under 1,500 lines.
- Preserve the moved tests' behavior and assertions.
- Run focused verification only; leave broad AWF/GitHub validation to AWF after
  agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Move the completed-monitor filesystem GC cleanup block, including its local
   helpers, from `test_monitor_completion_gc.py` into a new runtime test module.
2. Trim imports in both modules so each keeps only the dependencies it uses.
3. Run the moved/remaining targeted unit tests and the focused maintainability
   guard.
4. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py tests/unit/runtime/test_monitor_completion_filesystem_gc.py -q`
  - Passes, proving the split preserved test behavior.

Full AWF/GitHub validation is intentionally not run locally per the workspace
contract.
