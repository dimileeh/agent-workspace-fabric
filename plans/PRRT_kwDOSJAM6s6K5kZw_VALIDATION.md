# PRRT_kwDOSJAM6s6K5kZw Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K5kZw_PLAN.md`

## Requirement Status

- Verify the review claim against current code: Complete. The cited helper
  repaired agent runtime ownership before `adapter.run`, but repaired
  `core.hooksPath` only after `adapter.run` returned or raised.
- Add focused regression coverage for pre-launch mirror repair ordering:
  Complete. Added
  `test_protected_scope_repair_repairs_mirror_before_launch`.
- Add focused regression coverage for pre-launch mirror repair failure:
  Complete. Added
  `test_protected_scope_repair_fails_closed_when_prelaunch_mirror_repair_fails`.
- Preserve existing post-agent mirror cleanup and provider recovery behavior:
  Complete. The existing provider-retry regression now expects the new
  pre-launch repair plus the existing post-agent repair before provider recovery.
- Run targeted validation only: Complete. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `plans/PRRT_kwDOSJAM6s6K5kZw_PLAN.md`

Focused checks:

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_repairs_mirror_before_launch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_fails_closed_when_prelaunch_mirror_repair_fails -q`
  failed because mirror repair happened after `adapter.run`, and a pre-launch
  mirror repair failure still allowed `adapter.run` to execute.
- Final targeted regressions:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_repairs_mirror_before_launch tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_fails_closed_when_prelaunch_mirror_repair_fails tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_cleans_mirror_before_provider_retry -q`
  passed.
- Focused protected-scope repair file:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q`
  passed with 23 tests.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
  passed.

No remaining planned gaps.
