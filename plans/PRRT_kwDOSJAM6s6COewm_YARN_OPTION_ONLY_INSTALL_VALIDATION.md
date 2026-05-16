# PRRT_kwDOSJAM6s6COewm Yarn Option-Only Install Validation

Plan reference:
`plans/PRRT_kwDOSJAM6s6COewm_YARN_OPTION_ONLY_INSTALL_PLAN.md`

## Requirement Status

- Recognize Yarn option-only install shorthands such as `yarn --immutable`:
  Complete.
- Recognize the common CI combination `yarn --immutable --immutable-cache`:
  Complete.
- Preserve non-install Yarn probes, such as version/help-only commands, as
  non-dependency setup: Complete.
- Keep existing setup dependency network classifier behavior unchanged for other
  package managers: Complete.
- Add regression coverage for the PR review thread: Complete.

## Evidence

Files changed:

- `src/awf/runtime/validation.py`
- `tests/unit/runtime/test_validation.py`
- `plans/PRRT_kwDOSJAM6s6COewm_YARN_OPTION_ONLY_INSTALL_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6COewm_YARN_OPTION_ONLY_INSTALL_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k "yarn_option_only_installs or yarn_non_install_options"`
  - Initial run before implementation failed for the three Yarn option-only
    install cases.
  - Final run passed: 6 passed, 213 deselected.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  - Passed: 219 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf`
  - Passed.

Additional note:

- `uv run --python 3.12 --extra dev pytest tests/unit -q` was attempted as
  broader validation, but was stopped after reaching roughly 6% with no failures
  because it was too slow for this targeted review-thread fix cycle.

## Gaps

None.
