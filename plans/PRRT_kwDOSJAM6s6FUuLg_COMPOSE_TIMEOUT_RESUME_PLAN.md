# PRRT_kwDOSJAM6s6FUuLg Compose Timeout Resume Plan

## Problem Statement And Scope

The review thread reports that initial workspace launch honors the effective
Compose wait timeout from the profile and companion service requests, but PR
monitor resume still calls `ensure_project_up(..., wait=True)` without passing
that effective timeout. Scope is limited to monitor-resume compose restart
timeout selection and focused regression coverage.

## Requirements Checklist

- Recompute the same effective Compose up timeout for monitor resume from the
  persisted resolved profile and persisted companion task policy.
- Pass that timeout into `ComposeManager.ensure_project_up` so Docker Compose
  receives the same `--wait-timeout` used at initial launch.
- Preserve existing monitor resume behavior outside the timeout argument.
- Add focused regression coverage for a companion timeout greater than the
  default `300`.
- Run only targeted tests/checks; full AWF/GitHub validation remains owned by
  AWF after agent completion.

## Implementation Steps

1. Add a regression test around `WorkspaceExecutor.resume_pr_monitor` that seeds
   a monitoring PR with a persisted companion `compose_up_timeout_seconds=900`
   and asserts the resume compose call receives `900`.
2. Promote the existing stack-launcher effective timeout helper so both launch
   and resume can use the same calculation.
3. In monitor resume, rebuild the persisted profile context, load companion
   specs from `task_policy`, compute the effective timeout, and pass it to
   `ensure_project_up`.
4. Update narrow test doubles whose `ensure_project_up` signatures need to
   match the production call.
5. Run the focused regression test and a targeted lint/type check for the
   touched Python files.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py::TestExecutorCoverageEdgesPart002::test_resume_pr_monitor_preserves_companion_compose_timeout -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts -k resume_pr_monitor -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_uses_profile_compose_timeout tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_uses_effective_companion_compose_timeout -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py src/awf/node/stack_launcher.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_001.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_002.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_003.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_004.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_005.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_006.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_007.py tests/unit/node/test_stack_launcher.py`
- `uv run --python 3.12 --extra dev mypy src/awf/control/executor/monitor_handoff.py src/awf/node/stack_launcher.py`
