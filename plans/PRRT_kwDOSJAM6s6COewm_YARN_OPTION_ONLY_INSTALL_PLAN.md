# PRRT_kwDOSJAM6s6COewm Yarn Option-Only Install Plan

## Problem Statement and Scope

The setup dependency network classifier recognizes direct package-manager install
commands, but `yarn --immutable` and `yarn --immutable --immutable-cache` have no
positional subcommand after options. Modern Yarn treats those as install
invocations. The current command matcher returns `False` after skipping options,
which prevents the bounded dependency-network retry from applying.

Scope is limited to setup dependency command classification in
`src/awf/runtime/validation.py` and focused regression tests in
`tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Recognize Yarn option-only install shorthands such as `yarn --immutable`.
- Recognize the common CI combination `yarn --immutable --immutable-cache`.
- Preserve non-install Yarn probes, such as version/help-only commands, as
  non-dependency setup.
- Keep existing setup dependency network classifier behavior unchanged for other
  package managers.
- Add regression coverage for the PR review thread.

## Implementation Steps

1. Add failing regression tests for Yarn option-only install commands.
2. Add guard coverage for non-install Yarn option-only commands.
3. Update direct dependency command matching to treat selected Yarn install
   options as implicit install commands when no positional subcommand is present.
4. Run the narrow test file or targeted tests, then broader relevant validation if
   time and environment allow.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
