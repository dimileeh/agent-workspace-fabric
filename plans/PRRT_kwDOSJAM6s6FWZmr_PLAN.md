# PRRT_kwDOSJAM6s6FWZmr Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FWZmr` reports that optional companion
environment secrets rendered as `${SOURCE:-}` can change behavior on PR-monitor
resume. If the optional source env var existed during initial launch but is
missing when the monitor resumes, Docker Compose interpolates the persisted
fallback to an explicit empty target env var instead of preserving the
initial-launch behavior of omitting the optional secret.

Scope is limited to preserving optional companion env-secret omission during
monitor resume. Do not change branch management, pushing, broad validation, or
protected workflow/config files.

## Requirements Checklist

- Add a regression test showing monitor resume removes missing optional
  companion env-secret targets from the persisted compose file before compose
  restart.
- Keep present optional source env vars available through the existing
  placeholder path; do not persist raw secret values.
- Keep required companion env-secret behavior unchanged.
- Keep monitor resume tolerant of malformed companion policy by falling back to
  existing behavior instead of failing recovery.
- Run only focused checks for the changed behavior. Full AWF/GitHub validation
  is handled after agent completion.

## Implementation Steps

1. Add a focused resume regression test in the executor monitor-resume unit
   coverage.
2. Confirm the new test fails against current behavior when practical.
3. Implement a small resume-time helper that parses companion specs, identifies
   optional env-backed secrets whose source env var is absent, and removes only
   those target environment keys from the persisted compose YAML.
4. Invoke the helper before `ensure_project_up` during PR-monitor resume.
5. Run the targeted regression test and nearby companion secret tests.
6. Record validation in `plans/PRRT_kwDOSJAM6s6FWZmr_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k optional_companion`
  - Passes and demonstrates the missing optional target is omitted before
    compose restart.
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_companion_services.py -q -k environment_secret`
  - Passes and demonstrates initial companion secret resolution behavior remains
    intact.

Full repository validation, coverage, and CI-equivalent gates are intentionally
not run in this agent phase per the AWF workspace contract.
