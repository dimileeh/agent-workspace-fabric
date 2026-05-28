# Review PRRT_kwDOSJAM6s6FWqMl Optional Companion Env Resume Plan

## Problem Statement And Scope

PR review feedback reports that PR monitor resume permanently removes optional
companion environment secret placeholders from the persisted compose file when a
worker is temporarily missing the host env source. Since later monitor resumes
reuse that compose file, the optional credential is not restored when the source
env var becomes available again.

Scope is limited to monitor resume handling for optional companion env-backed
secrets in `src/awf/control/executor/monitor_handoff.py` and its targeted unit
coverage.

## Requirements Checklist

- Preserve the existing behavior that missing optional env-secret targets are
  omitted from the compose file used by `ensure_project_up`.
- Restore or retain present optional env-secret placeholders from task policy
  during monitor resume without writing raw secret values.
- Cover the missing-then-present resume case with a regression test.
- Keep validation focused; full AWF/GitHub validation is left to AWF after agent
  completion.

## Implementation Steps

1. Add a failing regression test proving a previously removed optional target is
   restored as `${OPTIONAL_TOKEN_SOURCE:-}` when the env source is present again.
2. Update the monitor resume compose refresh helper to remove missing optional
   targets and re-add present optional targets from companion task policy.
3. Run the targeted test(s) that exercise this behavior.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py -k "optional_companion_env_secret" -q`
  - Passes and demonstrates both missing optional omission and present optional
    restoration behavior.
