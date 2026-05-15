# PRRT_kwDOSJAM6s6CN-a7 NPM Workspace Flags Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6CN-a7` reports that setup dependency network
retry classification misses valid npm workspace install commands when the
workspace flag appears before the install subcommand, such as
`npm --workspace apps/console ci`. The fix is scoped to dependency setup command
detection in `src/awf/runtime/validation.py` and regression coverage in
`tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Treat `npm --workspace <name> ci` as a dependency setup command.
- Treat `npm -w <name> ci` as a dependency setup command.
- Preserve bounded retry behavior so unrelated commands still require specific
  dependency-output evidence.
- Keep the change focused on setup dependency command parsing.

## Implementation Steps

1. Add failing regression coverage for npm workspace flags before `ci`.
2. Extend dependency setup option parsing to skip npm workspace flag values
   before subcommand detection.
3. Run focused tests to prove the regression and the fix.
4. Save validation results in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier_accepts_npm_workspace_flags_before_subcommand`
  fails before the parser change and passes after it.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier`
  passes after the fix.
