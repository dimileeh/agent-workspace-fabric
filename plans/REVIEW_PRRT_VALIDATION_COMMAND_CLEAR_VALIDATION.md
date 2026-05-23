# Review PRRT_kwDOSJAM6s6ESXPh Validation Command Clear Validation

Plan reference: `plans/REVIEW_PRRT_VALIDATION_COMMAND_CLEAR_PLAN.md`

## Requirement Status

- Complete: Added `WorkspaceProfile.clear_validation_commands()` as an explicit
  clear operation while preserving empty append no-op behavior.
- Complete: `customize_project_onboarding_preview` clears validation commands
  when passed an explicit empty or all-whitespace `validation_commands`
  sequence.
- Complete: `validation_commands=None` remains a no-override path.
- Complete: Guided CLI passes `None` when the user does not enter a validation
  override, avoiding accidental clearing of detected commands.
- Complete: Added focused regression coverage for profile clearing, onboarding
  explicit clear behavior, and guided CLI no-override plumbing.

## Evidence

Files changed:

- `src/awf/profiles/models.py`
- `src/awf/profiles/onboarding.py`
- `src/awf/cli/main.py`
- `tests/unit/profiles/test_profiles.py`
- `tests/unit/profiles/test_project_onboarding.py`
- `tests/unit/cli/test_init.py`

Focused commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profiles.py -q -k 'validation_commands'`
  - Result: `3 passed, 122 deselected`
- `uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_project_onboarding.py -q -k 'validation_commands or customization'`
  - Result: `3 passed, 43 deselected`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k 'guided_egress_choices_follow_model_enum'`
  - Result: `1 passed, 134 deselected`
- `uv run --python 3.12 --extra dev ruff check src/awf/profiles/models.py src/awf/profiles/onboarding.py src/awf/cli/main.py tests/unit/profiles/test_profiles.py tests/unit/profiles/test_project_onboarding.py tests/unit/cli/test_init.py`
  - Result: `All checks passed!`

Full AWF/GitHub validation was not run in this agent phase; AWF owns broad
validation and merge-gating after completion.
