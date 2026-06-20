# PR614 Shard 6 Protected Repair Stub Validation

Plan reference: `plans/PR614_SHARD6_PROTECTED_REPAIR_STUB_PLAN.md`

## Requirement Status

- Keep the current AWF-managed branch and do not push: Complete.
- Do not edit workflow, quality-gate, or protected configuration files:
  Complete.
- Add the minimal `_rev_parse_head` method to the test stub: Complete.
- Preserve the assertion that unexpected adapter failure repairs hooks, checks
  HEAD, and raises `_MonitorHeadObjectMissingError`: Complete.
- Run the focused failing test after the change: Complete.
- Record that broad AWF/GitHub validation remains owned by AWF after
  completion: Complete.

## Evidence

Files changed:

- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py`
- `plans/PR614_SHARD6_PROTECTED_REPAIR_STUB_PLAN.md`
- `plans/PR614_SHARD6_PROTECTED_REPAIR_STUB_VALIDATION.md`

Focused checks run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_protected_scope_repair_checks_head_after_unexpected_adapter_failure -q`
  failed before the fix with `AttributeError: '_ProtectedRepairRunner' object
  has no attribute '_rev_parse_head'`, matching CI shard 6.
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_023.py::test_protected_scope_repair_checks_head_after_unexpected_adapter_failure -q`
  passed after the fix: 1 passed.

Full AWF/GitHub sharded coverage and broad CI validation were not run locally;
AWF owns those gates after agent completion.
