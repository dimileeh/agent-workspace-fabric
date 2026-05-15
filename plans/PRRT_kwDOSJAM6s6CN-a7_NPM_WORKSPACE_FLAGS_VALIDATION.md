# PRRT_kwDOSJAM6s6CN-a7 NPM Workspace Flags Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6CN-a7_NPM_WORKSPACE_FLAGS_PLAN.md`

## Requirement Status

- Complete: Treat `npm --workspace <name> ci` as a dependency setup command.
  - Evidence: Added regression coverage in
    `tests/unit/runtime/test_validation.py`; the focused test now passes.
- Complete: Treat `npm -w <name> ci` as a dependency setup command.
  - Evidence: Added the short-flag case to the same regression test; the parser
    now skips the flag value before detecting `ci`.
- Complete: Preserve bounded retry behavior so unrelated commands still require
  specific dependency-output evidence.
  - Evidence: The change is limited to value-taking option parsing in
    `_SETUP_DEPENDENCY_OPTION_VALUE_FLAGS`; the existing classifier test slice
    passes unchanged.
- Complete: Keep the change focused on setup dependency command parsing.
  - Evidence: Runtime changes are limited to
    `src/awf/runtime/validation.py`.

## Verification Evidence

- Before implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier_accepts_npm_workspace_flags_before_subcommand`
  - Result: failed with both workspace-flag cases returning
    `classification is None`.
- After implementation:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier_accepts_npm_workspace_flags_before_subcommand`
  - Result: `2 passed, 197 deselected`.
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier`
  - Result: `56 passed, 143 deselected`.
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  - Result: `199 passed`.
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Result: `All checks passed!`

## Gaps

None.
