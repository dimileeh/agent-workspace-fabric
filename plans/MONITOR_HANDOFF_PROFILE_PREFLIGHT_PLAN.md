# Monitor Handoff Profile Preflight Plan

## Problem Statement And Scope

PR monitor handoff setup currently runs profile `setup` and `pre_agent` phases but does not run the profile validation tool preflight that the normal executor runs immediately after setup. Adopted/release PR handoffs can therefore start a monitor for profiles whose validation commands are known to lose setup-installed tools.

Scope is limited to the monitor handoff setup path and focused regression coverage for the reported review thread.

## Requirements Checklist

- Run `run_profile_tool_preflight` for monitor handoff setup after successful profile setup/pre-agent phases.
- If preflight fails, mark the workspace failed before monitor startup.
- Preserve the existing profile-preflight diagnostic surface: profile resolution failure classification, redacted failure message, and preflight reason code when present.
- Keep setup dependency event recording behavior unchanged.
- Do not run broad AWF/GitHub-owned validation during the agent phase.

## Implementation Steps

1. Add a failing unit regression in monitor handoff setup coverage for a successful setup followed by a failing profile preflight.
2. Update `monitor_handoff_setup.py` to invoke optional `run_profile_tool_preflight` after successful setup/pre-agent execution.
3. Mark preflight failures through the existing setup failure path so handoff stops before monitor construction.
4. Run the narrow affected test file or selected tests only.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  - Passes all tests in the affected monitor handoff setup coverage file.

Full AWF/GitHub validation is owned by AWF after agent completion and is intentionally not run here.
