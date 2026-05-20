# Comment 4482045018 Plan

## Problem Statement And Scope

Address the review-level feedback from PR comment `issue:4482045018` without
changing AWF branch or push behavior. The actionable surface is limited to:

- `collect_core_readiness_report` should forward the resolved caller
  environment into the service status collector.
- `awf init` state-directory resolution must keep honoring a shell-level
  `AWF_HOST_WORK_DIR` during first-run compose env seeding.

## Requirements Checklist

- Add or update a regression test that fails before the readiness environment
  propagation fix.
- Update the readiness kwargs typing and status collector invocation so
  `environ` is forwarded consistently with the protocol.
- Verify the `awf init` state-directory path for shell `AWF_HOST_WORK_DIR` with
  compose assets present and no compose env state override.
- Keep changes scoped and do not weaken existing tests or assertions.
- Commit the fix locally with a conventional commit message tied to the review
  comment id.

## Implementation Steps

1. Update readiness unit coverage to assert that status collectors receive the
   caller-provided `environ` mapping.
2. Run the targeted readiness test and confirm the expected failure.
3. Add `environ` to `_StatusCollectorKwargs` and populate it in
   `status_kwargs`.
4. Add or run focused init coverage for shell `AWF_HOST_WORK_DIR` precedence
   during first-run compose env seeding.
5. Run narrow unit tests for readiness and init, then the relevant type check if
   practical.
6. Record validation results in `plans/COMMENT_4482045018_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py::test_core_readiness_resolves_provider_environment_from_compose_env_file -q`
  must pass after the implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_prefers_shell_host_work_dir_over_seeded_compose_env tests/unit/cli/test_init.py::test_init_without_path_uses_compose_env_host_work_dir_for_state_directory -q`
  must pass after implementation.
- `uv run --python 3.12 --extra dev mypy src/awf/service/readiness.py src/awf/cli/main.py`
  should pass, or any unrelated command limitation must be documented.
