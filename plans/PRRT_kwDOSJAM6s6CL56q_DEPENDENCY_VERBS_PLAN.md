# PRRT_kwDOSJAM6s6CL56q Dependency Verb Plan

## Problem Statement And Scope

The setup dependency network retry classifier currently treats any setup command
containing a known package-manager token as dependency setup. The review thread
reports that commands such as `npm run build`, `poetry run ...`, and
`bundle exec ...` can therefore be retried as dependency fetch failures even
when their subcommands are not dependency install or sync operations.

Scope is limited to the non-uv package-manager fast path in
`src/awf/runtime/validation.py` and focused regression coverage in
`tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Confirm the feedback against current code before editing.
- Add regression coverage proving non-install setup commands are not classified
  as setup dependency network failures.
- Preserve existing uv-specific behavior, including skipping `uv run`.
- Preserve classification for real dependency setup commands such as
  `pip install` and `npm ci`.
- Keep the change minimal and do not alter unrelated retry behavior.
- Validate with the narrowest relevant test command.

## Implementation Steps

1. Add tests for non-install commands that include known package-manager tools.
2. Replace the broad token-membership fast path with subcommand-aware matching
   for non-uv package managers.
3. Keep uv matching in its existing dedicated parser.
4. Run the targeted validation tests and update validation evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
