# Monitor Handoff Setup Failure Fallback Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6F-zZp` reports that when monitor handoff
setup fails and the final relayed `_mark_failed` call also raises, the handoff
logs and returns without a terminal workspace state. Scope is limited to the PR
monitor handoff setup-failure path in `monitor_handoff.py` and its focused unit
coverage.

## Requirements Checklist

- Preserve the setup failure reason, message, reason code, and details when the
  normal `_mark_failed` handoff path succeeds.
- If the final relayed `_mark_failed` raises and the workspace is still
  `running`, persist a terminal `failed` transition through the repository using
  the original setup failure payload.
- Do not start the PR monitor after setup failure.
- Respect stale/non-running workspaces by not forcing a terminal state over a
  newer status.
- Add a focused regression test for the setup-failure path where all normal
  `_mark_failed` attempts raise.

## Implementation Steps

1. Add a last-resort persistence helper in `src/awf/control/executor/monitor_handoff.py`
   that uses `WorkspaceRepository.transition_if_current` from `running` to
   `failed` and records the setup failure fields.
2. Invoke that helper after `_mark_failed_from_monitor_handoff_setup_failure`
   logs a final `_mark_failed` exception, only when the executor exposes a
   session factory.
3. Add a regression test in
   `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py`
   that monkeypatches `_mark_failed` to always raise and asserts the workspace
   still becomes `failed`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  must pass.
- Broad AWF/GitHub validation is intentionally not run in the agent phase; AWF
  owns full validation after completion.
