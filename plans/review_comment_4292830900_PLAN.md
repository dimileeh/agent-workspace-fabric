# Review Comment 4292830900 Plan

## Problem Statement and Scope

PR review comment `4292830900` requests:

- Aligning the new `workspace create` policy flags with underscore naming consistency in CLI option names.
- Ensuring MCP/CLI request construction avoids sending `null` task fields for optional policy keys.

The scope is limited to the `workspace create` CLI option names, corresponding CLI regression expectations, and contract registry coverage assertions.

## Requirements

- `workspace create` must accept `--out_of_scope_changes_json` and `--provider_recovery_json` options.
- JSON parsing behavior and validation failures for these options must remain unchanged.
- Contract metadata assertions must reflect the updated option names.
- No functional change is intended in MCP schema beyond preserving existing omission of `None` task fields.

## Implementation Steps

1. Update `src/awf/cli/main.py`:
   - Rename the two policy option long names from hyphenated to underscored variants.
   - Keep the same parse calls and payload inclusion logic.
2. Update CLI tests in `tests/unit/cli/test_cli.py`:
   - Use the new underscored CLI flag names in policy-flag tests.
3. Update parity registry in `tests/unit/contracts/_capabilities.py`:
   - Replace the old hyphenated option strings with underscored option strings for `create_workspace_v2`.

## Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py tests/unit/contracts/_capabilities.py -q`
