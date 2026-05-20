# Comment 4482045018 Validation

Plan reference: `plans/COMMENT_4482045018_PLAN.md`

## Requirement Status

- Complete: Added a readiness regression assertion that failed before the fix
  with `KeyError: 'environ'`.
- Complete: Updated `_StatusCollectorKwargs` and `collect_core_readiness_report`
  so the resolved `environ` mapping is forwarded to the status collector.
- Complete: Verified first-run `awf init` keeps honoring shell-level
  `AWF_HOST_WORK_DIR` when compose assets are present and the compose env file is
  seeded in the same invocation.
- Complete: Kept changes scoped to readiness propagation, focused init coverage,
  and the required plan/validation notes.
- Complete: Local commit prepared for the review comment fix.

## Evidence

- Changed `src/awf/service/readiness.py` to include and pass `environ` in status
  collector kwargs.
- Changed `tests/unit/service/test_readiness.py` to assert status collectors
  receive caller-provided `environ`.
- Changed `tests/unit/cli/test_init.py` to cover shell `AWF_HOST_WORK_DIR`
  precedence during first-run compose env seeding.

## Commands Run

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py::test_core_readiness_resolves_provider_environment_from_compose_env_file -q`
  failed before implementation with `KeyError: 'environ'`.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_prefers_shell_host_work_dir_over_seeded_compose_env -q`
  passed before implementation, confirming the state-directory concern was stale.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_readiness.py::test_core_readiness_resolves_provider_environment_from_compose_env_file -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_prefers_shell_host_work_dir_over_seeded_compose_env tests/unit/cli/test_init.py::test_init_without_path_uses_compose_env_host_work_dir_for_state_directory -q`
  passed after implementation.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/readiness.py src/awf/cli/main.py tests/unit/service/test_readiness.py tests/unit/cli/test_init.py`
  passed.
- `uv run --python 3.12 --extra dev mypy src/awf/service/readiness.py src/awf/cli/main.py`
  passed.

## Gaps

None.
