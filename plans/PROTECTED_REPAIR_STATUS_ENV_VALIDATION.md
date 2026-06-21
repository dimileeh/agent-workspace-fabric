# Protected Repair Status Env Validation

Plan reference: `plans/PROTECTED_REPAIR_STATUS_ENV_PLAN.md`

## Requirement Status

- Use `git_env_without_object_lookup_overrides()` for the post-repair `git status --porcelain` command: Complete.
- Preserve existing protected-scope repair behavior and result handling: Complete.
- Add focused regression coverage proving object lookup env vars are stripped from that status command: Complete.
- Run only targeted validation for the changed behavior: Complete.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_repair_protected.py`
- `tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py`
- `plans/PROTECTED_REPAIR_STATUS_ENV_PLAN.md`
- `plans/PROTECTED_REPAIR_STATUS_ENV_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py::test_protected_scope_repair_records_remaining_violations_after_agent_failure -q` — passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair_protected.py tests/unit/runtime/test_pr_monitor_runner_coverage_edges_parts/test_pr_monitor_runner_coverage_edges_part_008.py` — passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad validation and merge-gating after agent completion.
