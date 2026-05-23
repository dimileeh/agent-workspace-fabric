# PR 283 Review Comment 4524755400 Validation

Plan: `plans/PR283_REVIEW_COMMENT_4524755400_PLAN.md`

## Requirement Status

- Reject `awf init <path> --yes` without `--write-profile`: Complete.
- Keep `awf init <path> --write-profile --yes` working: Complete.
- Let guided onboarding collect more than one validation command: Complete.
- Preserve guided write confirmation as the final prompt: Complete.
- Record focused verification only: Complete.

## Evidence

- Updated `src/awf/cli/main.py` to fail fast when `--yes` is supplied without
  `--write-profile` in project-onboarding mode.
- Updated guided validation-command prompting in `src/awf/cli/main.py` to accept
  repeated commands before the final profile-write confirmation.
- Added focused regressions in `tests/unit/cli/test_init.py` for orphan `--yes`
  and multi-command guided validation input.

## Focused Verification

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_yes_requires_write_profile tests/unit/cli/test_init.py::test_init_guided_writes_answers_into_workspace_yml tests/unit/cli/test_init.py::test_init_guided_accepts_multiple_validation_commands -q`
  - Initial result before implementation: failed as expected.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_yes_requires_write_profile tests/unit/cli/test_init.py::test_init_write_profile_yes_creates_default_workspace_yml tests/unit/cli/test_init.py::test_init_guided_writes_answers_into_workspace_yml tests/unit/cli/test_init.py::test_init_guided_accepts_multiple_validation_commands tests/unit/cli/test_init.py::test_init_write_profile_guided_declined_confirmation_does_not_write -q`
  - Result after implementation: `5 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Result: passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/cli/main.py tests/unit/cli/test_init.py`
  - Result: passed.

Full AWF/GitHub validation remains owned by AWF after agent completion per the
workspace contract.
