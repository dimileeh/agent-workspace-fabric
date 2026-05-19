# Review 4482045018 Docker Host Order Validation

Plan reference: `plans/REVIEW_4482045018_DOCKER_HOST_ORDER_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving Compose interpolation of
  `DOCKER_HOST` cannot override an `AWF_DOCKER_HOST`-derived Docker host.
- Complete: Preserved existing Compose interpolation and Compose CLI env merge
  behavior by changing only the final assignment order for `DOCKER_HOST`.
- Complete: Ensured explicit `AWF_DOCKER_HOST` is applied after compose env
  merges, making it the final source for subprocess `DOCKER_HOST`.
- Complete: Confirmed `AWF_DOCKER_HOST` itself is still removed from the
  subprocess environment.

## Evidence

- Changed `src/awf/service/logs.py` to assign the explicit Docker host after
  `compose_env` and `compose_cli_env` are merged.
- Added
  `tests/unit/service/test_logs.py::test_service_logs_awf_docker_host_wins_over_compose_docker_host_interpolation`.
- Confirmed the new regression failed before the implementation change:
  `DOCKER_HOST` resolved to `unix:///compose-interpolation-docker.sock`.
- Passed: `uv run --python 3.12 --extra dev pytest tests/unit/service/test_logs.py -q`
- Passed: `uv run --python 3.12 --extra dev ruff check src/awf/service/logs.py tests/unit/service/test_logs.py`

## Gaps

No gaps remain for the planned scope.
