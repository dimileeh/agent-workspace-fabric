# PRRT_CLOyX UV Setup Dependency Plan

## Problem Statement and Scope

The setup dependency network classifier currently treats any command token named
`uv` as dependency setup. That can retry `uv run ...` setup scripts after generic
DNS or timeout failures and attach setup dependency metadata to script failures.

Scope is limited to the dependency-setup command classifier and focused unit
tests. No PR comments, pushes, or branch changes are in scope.

## Requirements Checklist

- Add a regression test proving `uv run python scripts/bootstrap.py` with a
  generic DNS failure is not classified as setup dependency network failure.
- Preserve classification for actual `uv` dependency setup commands such as
  `uv sync`.
- Narrow the `uv` command fast-path so only install/sync-style `uv` subcommands
  qualify by command name alone.
- Keep output-context fallback behavior for dependency/index-looking failures.
- Run targeted unit validation for the touched runtime validation tests.
- Commit the local fix with a conventional commit message tied to the review
  thread id.

## Implementation Steps

1. Add the `uv run` script regression test and confirm it fails before the code
   change.
2. Remove broad `uv` membership from dependency setup command tokens.
3. Add a small helper that recognizes only dependency-oriented `uv`
   subcommands.
4. Re-run the targeted validation tests.
5. Record validation evidence in `plans/PRRT_CLOyX_UV_SETUP_DEPENDENCY_VALIDATION.md`.
6. Stage only changed files and create a local commit.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  must pass.
