# PRRT_kwDOSJAM6s6ClF7f Agent Stack Docker Failure Validation

Plan reference: `PRRT_kwDOSJAM6s6ClF7f_AGENT_STACK_DOCKER_FAILURE_PLAN.md`

## Requirement Status

- Docker unavailability during compose `up` must fail launch even when the profile declares
  no required services: Complete. `ComposeStackLauncher.launch()` now always raises
  `WorkspaceServiceExecutionError` for `DOCKER_UNAVAILABLE`; the no-service regression covers
  the agent-only profile.
- Existing required service diagnostics must be preserved for serviceful and dind profiles:
  Complete. The required-services message still includes the required service list, and the full
  stack launcher test file passes.
- Non-Docker compose failures must keep their existing propagation behavior: Complete. The
  unchanged non-Docker propagation test passes in the full stack launcher test run.
- Add or update a regression test that fails before the implementation change: Complete. The
  updated no-service Docker-unavailable test failed before the launcher change and passed after it.
- Stage and commit only files changed for this thread: Complete. The final commit stages only the
  launcher, its focused test, and this thread's plan/validation files.

## Evidence

- Changed `src/awf/node/stack_launcher.py` to remove the silent `None` path on Docker
  unavailability and preserve Docker error details in the raised service error.
- Changed `tests/unit/node/test_stack_launcher.py` to expect failure for the generic/no-sidecar
  Docker-unavailable case.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py::test_compose_stack_launcher_fails_when_docker_missing_without_required_services -q`:
  first failed before implementation, then passed after implementation.
- Ran `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher.py -q`:
  22 passed.
- Ran `uv run --python 3.12 --extra dev ruff check src/awf/node/stack_launcher.py tests/unit/node/test_stack_launcher.py`:
  all checks passed.

## Gaps

No remaining implementation or validation gaps.
