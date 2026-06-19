# PRRT_kwDOSJAM6s6K5kZw Plan

## Problem Statement and Scope

The protected-scope repair path repairs agent runtime ownership before launching
the protected-scope repair agent, but only repairs the shared mirror
`core.hooksPath` after that agent returns. A concurrent poisoned mirror can
therefore affect commits made by the protected-scope repair agent itself.

Scope is limited to the protected-scope repair launch path in
`src/awf/runtime/pr_monitor_runner/remote_repair_protected.py` and focused unit
coverage for mirror repair ordering/fail-closed behavior.

## Requirements Checklist

- Verify the review claim against current code.
- Add focused regression coverage that mirror hook repair runs before the
  protected-scope repair adapter is launched.
- Add focused regression coverage that pre-launch mirror repair failure prevents
  adapter launch and fails closed.
- Preserve existing post-agent mirror cleanup and provider recovery behavior.
- Run only targeted validation; full AWF/GitHub validation remains managed by
  AWF after agent completion.

## Implementation Steps

1. Inspect the cited protected-scope repair helper and existing tests.
2. Add focused ordering and failure tests to the existing protected-scope repair
   coverage file.
3. Insert a mirror `core.hooksPath` repair/fail-closed check after runtime
   ownership repair and immediately before `adapter.run`.
4. Run the new focused tests and focused lint for changed files.
5. Record validation evidence in `plans/PRRT_kwDOSJAM6s6K5kZw_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_repairs_mirror_before_launch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_fails_closed_when_prelaunch_mirror_repair_fails -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- Pass criteria: the new regressions pass after the implementation, and focused
  lint passes.
