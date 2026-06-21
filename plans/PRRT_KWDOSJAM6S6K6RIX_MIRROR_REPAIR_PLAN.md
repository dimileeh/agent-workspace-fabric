# PRRT_kwDOSJAM6s6K6rix Mirror Repair Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6K6rix` reports that mirror `core.hooksPath`
repair runs before agent launch and inside the post-agent commit helper, but an
agent can poison the shared mirror and then exit through a post-agent path that
does not call the commit helper. This plan covers only the executor mirror
repair timing in `src/awf/control/executor/execution_flow.py` and focused
regression coverage.

## Requirements Checklist

- Verify the existing executor flow and confirm whether post-agent early returns
  can bypass mirror repair.
- Add focused regression coverage for a post-agent path that skips `_run_commit`.
- Run fail-closed mirror repair after an agent attempt returns and before
  post-agent early exits can proceed.
- Preserve existing pre-agent, cleanup-failure, and pre-commit repair behavior.
- Run only targeted validation; AWF/GitHub own broad validation after the agent
  phase.

## Implementation Steps

1. Add a test in `tests/unit/control/test_executor_mirror_hooks_path.py` for an
   agent that leaves no staged changes and a mirror repair failure immediately
   after the agent run.
2. Confirm the test fails against the current executor behavior.
3. Update `execution_flow.execute` to invoke the existing fail-closed mirror
   repair helper after agent return, before post-agent commit/capture gates.
4. Re-run the focused mirror-hooks test file.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_mirror_hooks_path.py -q`
  must pass.
- Full AWF/GitHub validation is intentionally not run in this workspace per the
  AWF workspace contract.
