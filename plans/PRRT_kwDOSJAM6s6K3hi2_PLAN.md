# PRRT_kwDOSJAM6s6K3hi2 Plan

## Problem Statement and Scope

The review thread reports that `_run_pre_push_validation_fix_pass` repairs a
poisoned shared mirror `core.hooksPath` only after the validation fix agent has
already run. A fix agent can self-commit before that post-run repair, bypassing
installed pre-commit hooks when the shared mirror is poisoned. Scope is limited
to the pre-push validation fix-pass mirror hook-path guard and focused unit
coverage for that ordering.

## Requirements Checklist

- Verify mirror hooks path repair currently occurs after the fix-agent launch.
- Repair the shared mirror hooks path before launching the validation fix agent.
- Preserve the post-agent mirror hooks repair as a race check.
- Fail closed with `MIRROR_HOOKS_PATH_POISONED` if the pre-launch repair fails,
  without launching the fix agent.
- Add focused regression coverage for the pre-launch ordering and post-run check.
- Run only targeted tests/checks for the changed behavior; broad AWF/GitHub
  validation remains managed after agent completion.

## Implementation Steps

1. Update the existing mirror-hooks fix-pass regression so it records repair and
   agent-launch ordering.
2. Confirm the updated regression fails against the current implementation.
3. Move the mirror hook-path repair guard before `adapter.run`, while leaving a
   second guard after `adapter.run`.
4. Update the fail-closed test to assert the agent is not launched when
   pre-launch repair fails.
5. Run the focused unit tests covering the changed behavior and targeted Ruff
   check on touched files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py -q -k "hooks_path or mirror_hooks_repair_failure"`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/pre_push_validation_fix_pass.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_020.py`

Pass criteria: the focused tests pass, targeted Ruff reports no issues, and no
broad AWF/GitHub-owned validation is run locally.
