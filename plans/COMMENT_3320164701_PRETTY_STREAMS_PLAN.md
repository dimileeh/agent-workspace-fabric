# COMMENT_3320164701_PRETTY_STREAMS_PLAN

## Problem Statement and Scope

The reserved `awf setup` and `awf start` pretty-mode placeholder errors print
human-readable non-zero guidance to stdout, while the analogous `awf init`
migration error prints to stderr. Scope is limited to aligning the pretty-mode
placeholder streams for `setup` and `start`; JSON output shape stays unchanged.

## Requirements Checklist

- Pretty `awf setup` placeholder guidance is emitted on stderr with empty stdout.
- Pretty `awf start` placeholder guidance is emitted on stderr with empty stdout.
- JSON `awf setup --format json` and `awf start --format json` output remains on stdout.
- Add focused regression coverage for the stream behavior.

## Implementation Steps

1. Update focused CLI unit tests to assert pretty placeholder output uses stderr.
2. Confirm the new assertions fail against the current implementation.
3. Change the pretty-mode `typer.echo` calls in setup/start placeholders to `err=True`.
4. Run the focused setup/start CLI unit tests.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py -q`
  passes after implementation.
- Full AWF/GitHub validation is intentionally left to AWF after agent completion,
  per workspace contract.
