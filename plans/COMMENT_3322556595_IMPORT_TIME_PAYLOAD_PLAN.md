# COMMENT_3322556595_IMPORT_TIME_PAYLOAD_PLAN

## Problem Statement and Scope

The reserved `awf setup` placeholder payload is currently constructed when
`src/awf/cli/setup_commands.py` is imported. That calls the first-run reason
catalog during CLI startup, so a missing placeholder reason code would break
imports for the broader `awf` CLI instead of only failing the affected command.
The same import-time construction exists in `src/awf/cli/start_commands.py`.

Scope is limited to deferring setup/start placeholder payload construction until
the corresponding command runs, while preserving the existing pretty and JSON
output shapes.

## Requirements Checklist

- `awf setup` no longer constructs its placeholder payload at module import.
- `awf start` no longer constructs its placeholder payload at module import.
- Setup/start command output behavior and JSON shape remain unchanged.
- Add focused regression coverage for import-time safety.

## Implementation Steps

1. Add a focused regression test that reloads the setup/start command modules
   with the payload builder patched to fail, proving imports do not construct
   placeholder payloads.
2. Run the focused CLI tests and confirm the new regression fails against the
   current implementation.
3. Move setup/start placeholder payload construction into command-time helper
   functions.
4. Run focused setup/start CLI tests plus targeted lint/type checks for the
   touched files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_first_run_command_imports.py tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py -q`
  passes after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/setup_commands.py src/awf/cli/start_commands.py tests/unit/cli/test_first_run_command_imports.py`
  passes after implementation.
- `uv run --python 3.12 --extra dev mypy src/awf/cli/setup_commands.py src/awf/cli/start_commands.py`
  passes after implementation.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion, per workspace contract.
