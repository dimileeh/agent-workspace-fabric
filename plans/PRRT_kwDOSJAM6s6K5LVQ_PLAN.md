# PRRT_kwDOSJAM6s6K5LVQ Plan

## Problem Statement and Scope

The protected-scope repair helper handles an `AgentRunError` by immediately
calling `_handle_provider_agent_run_error()`. That provider handler can raise a
retry, fallback, or auth control-flow exception before the helper performs its
post-agent status inspection, so a repair agent that poisons the shared mirror
`core.hooksPath` can strand the mirror in a poisoned state.

Scope is limited to `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
and focused unit coverage for this protected-scope repair failure path.

## Requirements Checklist

- Verify the review claim against current code.
- Add a focused regression test where protected-scope repair agent failure poisons
  the mirror and provider handling raises.
- Repair the shared mirror after the protected-scope repair agent returns or
  raises, before provider recovery handling can short-circuit.
- Preserve the existing provider recovery propagation semantics after the guard.
- Run only targeted validation for the changed behavior; broad AWF/GitHub
  validation remains managed by AWF after agent completion.

## Implementation Steps

1. Inspect the line-targeted protected-scope repair helper and nearby tests.
2. Add a regression to the existing protected-scope repair coverage file.
3. Import and use the existing mirror hooks-path repair helper in the protected
   repair helper's post-agent path.
4. Run the new focused test, then the focused test file if needed.
5. Record validation evidence in a matching validation document.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_cleans_mirror_before_provider_retry -q`
- Pass criteria: the new regression fails before the implementation and passes
  after it. If needed, the focused file passes for the touched protected-scope
  repair coverage.
