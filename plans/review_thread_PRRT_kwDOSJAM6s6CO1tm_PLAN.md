# PRRT_kwDOSJAM6s6CO1tm Plan

## Problem Statement And Scope

The setup dependency-network retry classifier should recognize valid pnpm
directory install commands such as `pnpm --dir apps/console install`. The
current dependency-tool subcommand scanner does not treat `--dir` as a
value-taking option, so it reads the directory path as the command verb and
misses transient registry/DNS setup failures.

Scope is limited to the dependency setup command detector and regression tests
for the reviewed pnpm `--dir` form.

## Requirements Checklist

- Add failing regression coverage for `pnpm --dir <path> install`.
- Preserve existing setup dependency retry behavior for other package manager
  commands.
- Keep the fix limited to option parsing so output fallback behavior remains
  unchanged.

## Implementation Steps

1. Add a unit test proving pnpm `--dir` install commands produce a setup
   dependency network classification on transient DNS output.
2. Run the targeted test to confirm it fails before the implementation change.
3. Teach the dependency-tool subcommand scanner that `--dir` consumes a value
   before the install subcommand.
4. Re-run the targeted test and a narrow validation surface around
   `tests/unit/runtime/test_validation.py`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py::<test> -q`
  fails before the code change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes after the implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes after the implementation.
