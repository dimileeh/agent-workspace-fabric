# COMMENT_3320164701_PRETTY_STREAMS_VALIDATION

Plan reference: `COMMENT_3320164701_PRETTY_STREAMS_PLAN.md`

## Requirement Status

- Complete: Pretty `awf setup` placeholder guidance is emitted on stderr with
  empty stdout.
- Complete: Pretty `awf start` placeholder guidance is emitted on stderr with
  empty stdout.
- Complete: JSON `awf setup --format json` and `awf start --format json` output
  remains on stdout; existing JSON shape assertions still pass.
- Complete: Focused regression coverage asserts the pretty-mode stream behavior.

## Evidence

Files changed:

- `src/awf/cli/setup_commands.py`
- `src/awf/cli/start_commands.py`
- `tests/unit/cli/test_setup_commands.py`
- `tests/unit/cli/test_start_commands.py`

Focused checks:

- Before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py -q`
  failed because pretty setup/start placeholder output was still on stdout.
- After implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py -q`
  passed with 6 tests.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/setup_commands.py src/awf/cli/start_commands.py tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py`
  passed.

Full AWF/GitHub validation was not run inside the agent phase; AWF owns broad
validation after completion per the workspace contract.
