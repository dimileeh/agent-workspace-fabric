# PRRT_kwDOSJAM6s6DAjXX Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6DAjXX_PLAN.md`

## Requirement Status

- Add regression test for subdirectory `awf init` seeded Compose env readiness:
  Complete. Added
  `test_init_without_path_passes_seeded_asset_root_env_to_bootstrap_readiness`.
- Preserve host-environment override behavior: Complete. The implementation
  uses `local_service_environ(env_file=env_file)`, preserving existing merge
  semantics where host environment overrides env-file values.
- Do not print seeded secret values: Complete. The regression asserts the
  seeded token is not present in CLI output.
- Keep project onboarding behavior unchanged: Complete. The change is scoped to
  `_run_init_service_bootstrap`.
- Run focused CLI init tests: Complete.

## Evidence

- Changed `src/awf/cli/main.py` to pass bootstrap readiness a provider
  environment built from the resolved init env file.
- Changed `tests/unit/cli/test_init.py` with a regression covering asset-root
  env seeding from a subdirectory.
- Verified the new regression failed before implementation with:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_passes_seeded_asset_root_env_to_bootstrap_readiness -q`
- Verified after implementation with:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py::test_init_without_path_passes_seeded_asset_root_env_to_bootstrap_readiness -q`
- Verified the relevant test surface with:
  `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
- Verified touched-file lint with:
  `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`

## Gaps

None.
