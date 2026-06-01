# Provisioner Line Limit CI Plan

## Problem Statement And Scope

PR #328 fails the maintainability guardrail because `src/awf/node/provisioner.py`
has grown to 1,599 lines, above the first-party file limit of 1,500 lines.
The fix must reduce the file size through real decomposition without disabling
or weakening the guardrail.

Scope is limited to moving standalone provisioner helper logic out of the
oversized module while preserving current imports and behavior.

## Requirements Checklist

- Reproduce the focused CI failure before editing.
- Keep `src/awf/node/provisioner.py` below the 1,500-line limit.
- Preserve existing imports of helper names from `awf.node.provisioner`.
- Do not weaken, skip, or modify the maintainability check.
- Run focused verification only; broad AWF/GitHub validation remains managed by
  AWF after agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Add a focused helper module under `src/awf/node/` for standalone provisioner
   helper functions.
2. Move the bottom-level helper implementations from `provisioner.py` into the
   new module.
3. Import the moved helpers into `provisioner.py` so existing references and
   tests continue to work.
4. Run the focused maintainability repro command.
5. Run targeted provisioner helper tests that import the moved names.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_core_decomposition_maintainability.py::test_first_party_code_files_stay_under_line_limit -q`
  - Passes with no oversized first-party files.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_provisioner_parts/test_provisioner_part_001.py -q`
  - Passes, confirming preserved helper imports and behavior.

Full AWF/GitHub validation and coverage gates are intentionally not run locally
per workspace contract.
