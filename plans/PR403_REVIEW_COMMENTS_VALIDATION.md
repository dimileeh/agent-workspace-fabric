# PR403 Review Comments Validation

Plan: `plans/PR403_REVIEW_COMMENTS_PLAN.md`

## Requirement Status

- Propagate the local Compose default API token into host CLI service settings:
  Complete. `resolve_service_settings()` now derives `ServiceSettings.api_token`
  from the merged local service environment when no explicit settings token is
  present.
- Prove console generated artifacts are excluded from the Docker build context:
  Complete. Added a static `.dockerignore` regression for console generated
  paths.
- Copy `next.config.ts` into the console runtime image:
  Complete. The runtime stage copies the config from the build stage.
- Exec `next` directly in the console runtime container:
  Complete. The image command now starts `./node_modules/.bin/next` directly.
- Use regular-file checks for env migration reads:
  Complete. Legacy and template paths use `is_file()`, and a root `.env`
  directory is rejected before parsing.
- Preserve migrated env values containing `$`:
  Complete. AWF-managed dotenv reads use `interpolate=False`; migrated
  `$`-containing values are written with single quotes so generated `.env`
  content is not shell-expanded by accident.
- Keep `awf start` / service bootstrap console startup out of this fix:
  Complete. No bootstrap startup behavior was changed.
- Avoid removing the intentional `apps/console/lib` sdist force-include:
  Complete. Added a comment explaining why the force include remains.

## Evidence

- Changed files:
  - `apps/console/Dockerfile`
  - `pyproject.toml`
  - `src/awf/service/config.py`
  - `src/awf/service/env_migration.py`
  - `src/awf/service/environment.py`
  - `tests/unit/service/test_config_parts/test_config_part_001.py`
  - `tests/unit/service/test_config_parts/test_config_part_003.py`
  - `tests/unit/service/test_env_migration.py`
  - `tests/unit/service/test_environment.py`
  - `tests/unit/test_console_dockerfile.py`

## Commands Run

- Initial red checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_003.py -q -k api_token`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_env_migration.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/test_console_dockerfile.py -q`
- Green checks:
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_003.py -q -k api_token`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_env_migration.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/test_console_dockerfile.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_environment.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_packaging.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py -q`
  - `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_001.py tests/unit/service/test_config_parts/test_config_part_003.py tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py tests/unit/test_console_dockerfile.py tests/unit/cli/test_packaging.py tests/unit/cli/test_package_build_contents.py -q`
    - Result: 196 passed.
  - `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py src/awf/service/env_migration.py src/awf/service/environment.py tests/unit/service/test_config_parts/test_config_part_001.py tests/unit/service/test_config_parts/test_config_part_003.py tests/unit/service/test_env_migration.py tests/unit/service/test_environment.py tests/unit/test_console_dockerfile.py`
  - `uv run --python 3.12 --extra dev mypy src/awf/service/config.py src/awf/service/env_migration.py src/awf/service/environment.py`
  - `docker build -t awf-console:ci-check -f apps/console/Dockerfile .`
  - `docker image inspect awf-console:ci-check --format '{{json .Config.Cmd}}'`
    - Result: `["./node_modules/.bin/next","start","--hostname","0.0.0.0","--port","3000"]`
  - `git diff --check development`

## Residual Notes

- The console bootstrap review thread was evaluated as a legitimate product
  improvement but intentionally deferred because the user explicitly asked to
  leave the `awf start` route unchanged in this PR.
- `docker build` printed npm audit warnings from existing console dependencies
  but completed successfully.
