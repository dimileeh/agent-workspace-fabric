# PRRT_kwDOSJAM6s6K5LVQ Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K5LVQ_PLAN.md`

## Requirement Status

- Verify the review claim against current code: Complete. The helper called
  `_handle_provider_agent_run_error()` inside the `except AgentRunError` path,
  before any mirror cleanup could run.
- Add a focused regression for provider short-circuit after mirror poisoning:
  Complete. Added
  `test_protected_scope_repair_cleans_mirror_before_provider_retry`.
- Repair the shared mirror before provider recovery can short-circuit:
  Complete. `_repair_protected_scope_changes_before_commit()` now calls
  `repair_mirror_hooks_path()` for the backing mirror before invoking provider
  error handling.
- Preserve provider recovery propagation after the guard: Complete. The
  regression still expects `ProviderRecoveryRetryError` to propagate after mirror
  repair runs.
- Run targeted validation only: Complete. Full AWF/GitHub validation remains
  managed by AWF after agent completion.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `plans/PRRT_kwDOSJAM6s6K5LVQ_PLAN.md`

Focused checks:

- Initial red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_cleans_mirror_before_provider_retry -q`
  failed because events were `["provider-recovery"]` instead of
  `["mirror-repair", "provider-recovery"]`.
- Final targeted regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_cleans_mirror_before_provider_retry -q`
  passed.
- Focused protected-scope repair file:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py -q`
  passed with 20 tests.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
  passed.

No remaining planned gaps.
