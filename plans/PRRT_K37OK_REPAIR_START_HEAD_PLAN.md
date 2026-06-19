# PRRT K37OK Repair Start Head Plan

## Problem Statement and Scope

Inline review thread `PRRT_kwDOSJAM6s6K37OK` reports that repair operation
start HEAD capture runs `git rev-parse HEAD` with the ambient environment.
Inherited Git object lookup overrides could make the captured baseline invalid
for the canonical mirror, then thread that bad SHA through rollback, recovery,
and push validation.

Scope is limited to sanitizing the repair-start baseline command and adding a
focused regression test for that behavior.

## Requirements Checklist

- Verify the review claim against `src/awf/runtime/pr_monitor_runner/remote_repair.py`.
- Add a focused regression test before implementation.
- Sanitize inherited Git object lookup overrides for repair-start `rev-parse HEAD`.
- Keep changes scoped to the review feedback.
- Run only targeted validation for the changed behavior.

## Implementation Steps

1. Add a unit test for `_repair_operation_start_head_result` proving the
   worktree `rev-parse HEAD` call receives an environment without
   `GIT_OBJECT_DIRECTORY` or `GIT_ALTERNATE_OBJECT_DIRECTORIES`.
2. Run the new targeted test and confirm it fails before implementation.
3. Pass `git_env_without_object_lookup_overrides()` to the repair-start
   `rev-parse HEAD` runner call.
4. Re-run the targeted test.
5. Record validation evidence in `plans/PRRT_K37OK_REPAIR_START_HEAD_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q -k repair_operation_start_head`
  - Pass criteria: the new regression and nearby repair-start fallback test pass.

Full AWF/GitHub validation is managed by AWF after agent completion and will not
be run in this agent phase.
