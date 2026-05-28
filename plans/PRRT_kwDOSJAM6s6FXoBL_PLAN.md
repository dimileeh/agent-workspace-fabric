# PRRT_kwDOSJAM6s6FXoBL Plan

## Problem Statement and Scope

The PR review reports that monitor resume refreshes optional companion env
secrets by parsing and re-dumping the persisted Compose file with PyYAML. When
the file also contains required Compose placeholders such as
`${VAR:?reason: detail}`, PyYAML can emit those values as single-quoted scalars,
which Docker Compose treats as literal text instead of interpolating.

Scope is limited to the resume-time optional companion env-secret refresh in
`src/awf/control/executor/monitor_handoff.py` and a focused regression test.

## Requirements Checklist

- Add a regression test showing required Compose interpolation placeholders are
  not rewritten as single-quoted literals when an optional secret is omitted or
  restored.
- Preserve existing optional-secret behavior: remove missing optional env
  targets and restore present optional placeholders without writing raw secret
  values.
- Keep Compose interpolation active for rewritten `${...}` scalars.
- Avoid broad AWF/GitHub-owned validation; run only targeted tests/checks.
- Commit the fix locally on the current AWF-managed branch.

## Implementation Steps

1. Add a focused unit test near the existing monitor resume env-secret refresh
   tests.
2. Run that test and confirm it fails against the current implementation when
   practical.
3. Update the resume compose dump path to force Compose interpolation scalars
   to double-quoted YAML rather than single-quoted YAML.
4. Re-run the targeted test and adjacent optional-secret refresh tests.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6FXoBL_VALIDATION.md`.
6. Stage only changed files and commit with a conventional commit message tied
   to the review thread.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -q -k 'companion_env_secret_refresh or optional_companion_env_secret'`
  - Passes after implementation.
- No full coverage, whole-repository tests, frontend builds, push, branch
  switch, or CI-equivalent validation is run by this agent phase.
