# PRRT_kwDOSJAM6s6DC_rM Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DC_rM_PLAN.md`

## Requirement Status

- Complete: Add a regression test proving bootstrap Docker stages receive
  `DOCKER_HOST` when the merged service environment contains only
  `AWF_DOCKER_HOST`.
  Evidence: `test_bootstrap_mirrors_awf_docker_host_to_docker_cli_environment`
  fails before implementation and passes after the bootstrap environment fix.
- Complete: Preserve existing behavior when no `AWF_DOCKER_HOST` is present.
  Evidence: the full `tests/unit/service/test_bootstrap.py` suite passes,
  including the empty environment regression.
- Complete: Keep the readiness/status poll environment aligned with the stage
  environment.
  Evidence: the new regression asserts both subprocess stage env and collected
  provider env include the mirrored `DOCKER_HOST`.
- Complete: Avoid broad refactors or changes to CLI branch/push behavior.
  Evidence: changes are limited to bootstrap environment normalization, the
  focused test, and plan/validation records.

## Files Changed

- `src/awf/service/bootstrap.py`
- `tests/unit/service/test_bootstrap.py`
- `plans/PRRT_kwDOSJAM6s6DC_rM_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6DC_rM_VALIDATION.md`

## Verification

Initial failing regression:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::test_bootstrap_mirrors_awf_docker_host_to_docker_cli_environment -q
```

Result before implementation: failed because `DOCKER_HOST` was missing from the
collector/stage environment.

Passing verification:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py::test_bootstrap_mirrors_awf_docker_host_to_docker_cli_environment -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_bootstrap.py -q
uv run --python 3.12 --extra dev ruff check src/awf/service/bootstrap.py tests/unit/service/test_bootstrap.py
uv run --python 3.12 --extra dev mypy src/awf/service/bootstrap.py
```

All passing verification commands completed successfully.
