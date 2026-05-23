# Review PRRT_kwDOSJAM6s6ESXPh Validation Command Clear Plan

## Problem Statement and Scope

The onboarding customization path ignores an explicit empty `validation_commands`
sequence because it only calls `WorkspaceProfile.with_validation_commands` when
the cleaned command list is non-empty. Review feedback asks that an explicit
empty or all-whitespace list clear detected validation commands.

Scope is limited to onboarding profile customization and directly related model
helper behavior. Existing append semantics for
`WorkspaceProfile.with_validation_commands([])` remain unchanged because tests
and request-resolution behavior treat it as a no-op.

## Requirements Checklist

- Add an explicit profile operation that clears validation commands.
- Make `customize_project_onboarding_preview(..., validation_commands=[])` and
  all-whitespace sequences clear existing validation commands.
- Preserve `validation_commands=None` as no override.
- Preserve guided CLI behavior so absence of user-entered validation commands
  does not accidentally clear already detected commands.
- Add focused regression tests for model clear behavior, onboarding explicit
  clear behavior, and guided CLI no-override behavior.

## Implementation Steps

1. Add `WorkspaceProfile.clear_validation_commands()`.
2. Update onboarding customization to call the clear method when an explicit
   sequence cleans to empty.
3. Update guided CLI prompt plumbing to pass `None` when there is no validation
   override.
4. Add targeted unit coverage in profile and CLI tests.

## Verification Commands

Focused checks only:

- `uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_profiles.py -q -k 'validation_commands'`
- `uv run --python 3.12 --extra dev pytest tests/unit/profiles/test_project_onboarding.py -q -k 'validation_commands or customization'`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k 'guided_egress_choices_follow_model_enum'`

Full AWF/GitHub validation is intentionally not run during this agent phase.
