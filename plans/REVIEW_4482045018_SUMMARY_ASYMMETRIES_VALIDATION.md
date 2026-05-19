# Validation: Address Review 4482045018 Summary Asymmetries

Plan reference: `plans/REVIEW_4482045018_SUMMARY_ASYMMETRIES_PLAN.md`

## Requirement Status

- Complete: Added a regression proving `service logs` removes stale caller
  Docker host case variants when `AWF_DOCKER_HOST` supplies the Docker client
  host.
- Complete: Added a regression proving `service logs` removes stale caller
  Docker host case variants when the resolved service environment supplies
  `DOCKER_HOST`.
- Complete: Preserved existing behavior where `service logs` honors
  `DOCKER_HOST` from the resolved service environment when `AWF_DOCKER_HOST` is
  absent; the new fix canonicalizes the exported subprocess key instead of
  removing that fallback.
- Complete: Added a regression proving bootstrap treats an absolute path to
  `asset_root / docker/compose/local-service.yml` as the default compose asset
  branch.
- Complete: No branch switch, push, rebase, force-push, or destructive git
  operation was used.

## Evidence

Files changed:

- `src/awf/service/logs.py`
- `src/awf/service/bootstrap.py`
- `tests/unit/service/test_logs.py`
- `tests/unit/service/test_bootstrap.py`
- `plans/REVIEW_4482045018_SUMMARY_ASYMMETRIES_PLAN.md`
- `plans/REVIEW_4482045018_SUMMARY_ASYMMETRIES_VALIDATION.md`

Red phase:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_stale_caller_docker_host_variants_when_awf_host_is_forced tests/unit/service/test_logs.py::test_service_logs_removes_stale_caller_docker_host_variants_when_docker_host_is_resolved tests/unit/service/test_bootstrap.py::test_bootstrap_treats_absolute_asset_root_compose_path_as_default -q`
- Result before implementation: failed as expected with stale mixed-case
  `DoCkEr_HoSt` surviving in logs subprocess env and bootstrap routing the
  absolute default compose file through `_resolve_user_path`.

Green phase:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py::test_service_logs_removes_stale_caller_docker_host_variants_when_awf_host_is_forced tests/unit/service/test_logs.py::test_service_logs_removes_stale_caller_docker_host_variants_when_docker_host_is_resolved tests/unit/service/test_bootstrap.py::test_bootstrap_treats_absolute_asset_root_compose_path_as_default -q`
- Result after implementation: 3 passed.

Focused verification:

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py tests/unit/service/test_bootstrap.py -q`
- Result: 69 passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py src/awf/service/bootstrap.py tests/unit/service/test_logs.py tests/unit/service/test_bootstrap.py`
- Result: All checks passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/service/logs.py src/awf/service/bootstrap.py tests/unit/service/test_logs.py tests/unit/service/test_bootstrap.py`
- Result: 4 files already formatted.

## Remaining Gaps

None.
