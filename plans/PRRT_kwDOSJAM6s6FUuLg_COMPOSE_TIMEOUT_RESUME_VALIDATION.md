# PRRT_kwDOSJAM6s6FUuLg Compose Timeout Resume Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6FUuLg_COMPOSE_TIMEOUT_RESUME_PLAN.md`

## Requirement Status

- Complete: Recomputed the monitor-resume Compose timeout from the persisted
  profile and persisted companion task policy.
  Evidence: `src/awf/control/executor/monitor_handoff.py`,
  `src/awf/node/stack_launcher.py`.
- Complete: Passed the effective timeout into
  `ComposeManager.ensure_project_up` during PR-monitor resume.
  Evidence: `src/awf/control/executor/monitor_handoff.py`.
- Complete: Preserved existing monitor resume behavior outside the timeout
  argument.
  Evidence: focused resume regression slice passed.
- Complete: Added regression coverage for a companion timeout above `300`.
  Evidence:
  `tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py`.
- Complete: Used focused validation only. Full AWF/GitHub validation remains
  owned by AWF after agent completion.

## Validation Evidence

- Failing-first evidence before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::TestExecutorCoverageEdgesPart002::test_resume_pr_monitor_preserves_companion_compose_timeout -q`
  failed with `assert 300 == 900`.
- Passing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::TestExecutorCoverageEdgesPart002::test_resume_pr_monitor_preserves_companion_compose_timeout -q`
  passed.
- Resume slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts -k resume_pr_monitor -q`
  passed: 17 passed, 106 deselected.
- Stack-launcher timeout slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_uses_profile_compose_timeout tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_uses_effective_companion_compose_timeout -q`
  passed: 4 passed.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py src/awf/node/stack_launcher.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_001.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_002.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_003.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_004.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py tests/unit/node/test_stack_launcher.py`
  passed.
- Targeted type check:
  `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py src/awf/node/stack_launcher.py`
  passed.

## Gaps

No planned requirements remain open. Broad validation, full coverage, and CI
gates were intentionally not run in the agent phase per the AWF workspace
contract.
