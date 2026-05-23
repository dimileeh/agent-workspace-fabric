# Review 4350229871 Secret Assertion Plan

## Problem Statement and Scope

CodeRabbit review comment `4350229871` reported one still-valid outside-diff
test reliability issue: `tests/unit/cli/test_init.py` asserts that the generic
substring `"root"` is absent from CLI output after a non-UTF-8 env overlay
merge failure. That can fail for unrelated output containing `root`.

Other review items in the supplied review-level summary are already addressed
in the current branch: project onboarding docs use `awf init . --write-profile
--yes`, guided init rejects non-interactive stdio, guided decline does not write
the profile, and explicit empty validation command lists clear validation
commands through `clear_validation_commands()`.

## Requirements Checklist

- Replace the fragile generic `"root"` output assertion with a targeted secret
  leak assertion for the exact fixture value.
- Preserve existing regression coverage and assertions around the merge failure.
- Keep changes scoped to the affected test plus required plan/validation docs.
- Run only focused validation for the affected behavior; leave broad AWF/GitHub
  validation to the AWF post-agent workflow.

## Implementation Steps

1. Update the affected assertion in `tests/unit/cli/test_init.py` to check that
   `AWF_API_TOKEN=root` is not leaked.
2. Run the single affected unit test.
3. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q -k test_init_without_path_json_marks_non_utf8_env_overlay_merge_failed`
  - Passes with the updated targeted assertion.

Full repository validation, coverage gates, and CI-equivalent checks are owned
by AWF/GitHub after agent completion for this repair cycle.
