# PR614 Shard 6 Protected Repair Stub Plan

## Problem Statement and Scope

CI run `27862959455` fails in `python-coverage-shards (6)` at
`tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_protected_scope_repair_checks_head_after_unexpected_adapter_failure`.

The focused local repro fails with `AttributeError` because the test-only
`_ProtectedRepairRunner` stub lacks `_rev_parse_head`, which the production
protected-scope repair path now calls before launching the repair agent.

Scope is limited to updating the test stub to satisfy the current helper
contract while preserving the test's missing-HEAD behavior assertion.

## Requirements Checklist

- Keep the current AWF-managed branch and do not push.
- Do not edit workflow, quality-gate, or protected configuration files.
- Add the minimal `_rev_parse_head` method to the test stub.
- Preserve the assertion that unexpected adapter failure repairs hooks, checks
  HEAD, and raises `_MonitorHeadObjectMissingError`.
- Run the focused failing test after the change.
- Record that broad AWF/GitHub validation remains owned by AWF after completion.

## Implementation Steps

1. Add `_rev_parse_head` to `_ProtectedRepairRunner`.
2. Return `None` so this test remains focused on the existing missing-HEAD
   verification path rather than pre-repair-head reset behavior.
3. Run the single failing test.
4. Save validation evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_protected_scope_repair_checks_head_after_unexpected_adapter_failure -q`
  passes.
- Full sharded coverage and broad GitHub checks are not run locally; AWF/GitHub
  owns those gates after this agent phase.
