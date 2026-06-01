# Monitor Handoff Ownership Repair Plan

## Problem Statement and Scope

The PR monitor handoff setup helper runs profile `setup` and `pre_agent`
phases for adopted/release PR monitors. Unlike the normal executor setup path,
it does not first repair agent runtime ownership. On local Linux/root
control-plane deployments, root-owned runtime files such as `.venv` can cause
setup commands to fail before the monitor starts.

Scope is limited to the handoff setup helper and focused regression coverage for
review thread `PRRT_kwDOSJAM6s6F9jQc` / comment `3330714584`.

## Requirements Checklist

- Add runtime ownership repair before monitor handoff profile setup phases run.
- Use the same ownership repair reason code and executor event name as the
  normal executor setup path.
- If ownership repair fails, mark the workspace failed as infrastructure failure
  and do not run profile setup or the monitor.
- Preserve existing setup failure, exception, and dependency-network behavior.
- Add focused unit regression coverage.

## Implementation Steps

1. Add failing tests in the monitor handoff setup coverage file for repair order
   and repair-failure blocking behavior.
2. Import and call `repair_agent_runtime_ownership` in
   `monitor_handoff_setup.py` before `run_profile_phases`.
3. Reuse `AGENT_RUNTIME_OWNERSHIP_REPAIR_FAILED_REASON_CODE` and
   `EXECUTOR_AGENT_RUNTIME_OWNERSHIP_REPAIR_EVENT_NAME`.
4. Run only focused tests for the touched monitor handoff setup behavior.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally left to AWF after agent
  completion per the workspace contract.
