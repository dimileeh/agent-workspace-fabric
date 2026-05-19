# Init Seeded Preflight Plan

## Problem Statement And Scope

The `awf init` bootstrap path currently resolves local service settings and runs
the Docker doctor preflight before copying a missing env file from its example.
On a first run, Docker-related values present only in `.env.example`, such as
`AWF_DOCKER_HOST`, are invisible to the preflight and settings resolution.

Scope is limited to the no-path service bootstrap mode in `src/awf/cli/main.py`
and focused unit coverage in `tests/unit/cli/test_init.py`.

## Requirements Checklist

- [ ] Seed the resolved env file before loading `local_service_environ`,
      constructing `Settings(_env_file=...)`, or running the Docker preflight.
- [ ] Preserve `--no-write-env` behavior.
- [ ] Preserve existing pretty and JSON reporting for env seeding actions and
      failures without printing secret values.
- [ ] Ensure bootstrap provider readiness continues to receive the same
      post-seed env view used for settings and preflight.
- [ ] Keep Docker failure handling and state directory creation ordering intact
      after the seeded env has been considered.

## Implementation Steps

1. Add a failing unit regression where an asset-root compose `.env.example`
   contains `AWF_DOCKER_HOST`, no `.env` exists, and the doctor preflight must
   see that host value through both settings and environment kwargs.
2. Move env seeding in `_run_init_service_bootstrap` to occur immediately after
   `_resolve_init_env_paths()` and before `local_service_environ` and
   `Settings(_env_file=...)`.
3. Remove the redundant post-seed env reload for bootstrap provider readiness or
   make it share the already resolved seeded env.
4. Run the focused init unit tests, then broader lint/type/test checks if the
   touched surface warrants it.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_init.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/cli/main.py tests/unit/cli/test_init.py`
  passes.
- `uv run --python 3.12 --extra dev mypy src/awf`
  passes or any unrelated environment issue is documented.
