# REVIEW_PRRT_KWDOSJAM6S6K8LRG Repair Dirty Env Validation

Plan reference: `REVIEW_PRRT_KWDOSJAM6S6K8LRG_REPAIR_DIRTY_ENV_PLAN.md`

## Requirement Status

- Confirm whether the current dirty guard passes a sanitized git environment: Complete.
  The reported `git status --porcelain --untracked-files=all` call did not pass `env`.
- Pass `git_env_without_object_lookup_overrides()` to that `git status` call: Complete.
  `src/awf/runtime/pr_monitor_runner/remote_repair.py` now sanitizes the status probe.
- Add a focused regression proving object lookup override variables are not inherited: Complete.
  `tests/unit/runtime/test_agent_runtime_memory_repair_guard.py` asserts both object lookup
  override keys are absent from the recorded command environment.
- Run only targeted tests for the changed behavior: Complete.

## Evidence

- Changed files:
  - `src/awf/runtime/pr_monitor_runner/remote_repair.py`
  - `tests/unit/runtime/test_agent_runtime_memory_repair_guard.py`
- Focused verification:
  - `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_agent_runtime_memory_repair_guard.py -q`
  - Result: `7 passed in 7.52s`
  - `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_agent_runtime_memory_repair_guard.py`
  - Result: `All checks passed!`

Full AWF/GitHub validation, coverage gates, and merge checks are intentionally left to
AWF after agent completion per the workspace contract.
