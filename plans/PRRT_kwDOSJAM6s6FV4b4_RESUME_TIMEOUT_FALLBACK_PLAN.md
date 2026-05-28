# PRRT_kwDOSJAM6s6FV4b4 Resume Timeout Fallback Plan

## Problem Statement And Scope

The PR monitor resume path initializes `compose_up_timeout_seconds` to `300` and then resolves the workspace profile, companion specs, and effective compose timeout inside one `try` block. If profile resolution succeeds but companion timeout resolution fails, the compose restart falls back to `300` instead of the profile's own `docker.startup_timeout_seconds`.

Scope is limited to `resume_pr_monitor` timeout fallback behavior and its regression coverage.

## Requirements Checklist

- Add a regression test proving a resolved profile timeout is preserved when companion policy parsing fails during PR monitor resume.
- Keep companion timeout overrides unchanged when they resolve successfully.
- Keep the hardcoded `300` fallback only for cases where profile resolution itself fails or no profile timeout is available.
- Run focused tests only; broad AWF/GitHub validation remains managed by AWF after agent completion.

## Implementation Steps

1. Add a targeted unit test near existing `resume_pr_monitor` timeout tests.
2. Confirm the new test fails against the current implementation.
3. Update `resume_pr_monitor` so the profile timeout becomes the fallback immediately after profile recovery succeeds, before companion override resolution.
4. Re-run the focused unit tests covering the new fallback and the existing companion timeout behavior.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k "resume_pr_monitor_preserves_profile_compose_timeout_when_companion_resolution_fails or resume_pr_monitor_preserves_companion_compose_timeout"`

Pass criteria: selected tests pass, including the new regression and the existing companion override test.
