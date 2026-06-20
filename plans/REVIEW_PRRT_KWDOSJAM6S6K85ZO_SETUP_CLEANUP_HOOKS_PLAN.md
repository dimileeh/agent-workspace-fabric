# Review PRRT_kwDOSJAM6s6K85zO Setup Cleanup Hooks Plan

## Problem Statement And Scope

The inline review reports that when profile setup/pre-agent execution raises
`ComposeExecCleanupError`, control jumps to the outer cleanup-error handler
before the post-setup hooks-path repairs can run. If setup/pre-agent commands
poison the shared mirror `core.hooksPath`, sibling workspaces can inherit that
bad mirror state.

Scope is limited to repairing the mirror hooks path on setup/pre-agent
`ComposeExecCleanupError` in `src/awf/control/executor/execution_flow.py` and
adding focused regression coverage.

## Requirements Checklist

- Confirm the setup/pre-agent cleanup-error path currently bypasses post-setup
  mirror hooks repair.
- Add a regression test covering `run_profile_phases(... setup/pre_agent ...)`
  raising `ComposeExecCleanupError`.
- Ensure the executor attempts mirror hooks repair after that setup cleanup
  failure before marking the workspace failed.
- Preserve existing cleanup-failure classification and failure message behavior.
- Keep validation focused; full AWF/GitHub validation remains managed after the
  agent exits.

## Implementation Steps

1. Add a targeted unit regression in the executor test area.
2. Run the new test and confirm it fails before the implementation change.
3. Wrap the setup/pre-agent `run_profile_phases` call so
   `ComposeExecCleanupError` triggers best-effort mirror hooks repair, then
   re-raises to the existing outer handler.
4. Run the focused regression test, and if needed a narrow adjacent test file.
5. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts/test_executor_part_007.py -k setup_cleanup_error_repairs_mirror_hooks_path -q`
  - Passes after the fix and fails before it.
- No broad AWF/GitHub validation suite is run in the agent phase.
