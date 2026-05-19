# Review 4482312648 Config Resolution Plan

## Problem Statement and Scope

Greptile flagged two outside-diff config risks in PR #265:

- `Settings` stores constructor-explicit field names in both Pydantic private
  storage and the instance `__dict__` via `object.__setattr__`, which is fragile
  for a frozen Pydantic settings model.
- `resolve_local_service_compose_env_file()` walks module-path ancestors all the
  way to filesystem root and can accidentally load an unrelated
  `docker/compose/.env` above an installed package location.

Scope is limited to settings explicitness tracking, local service compose env
file discovery, and regression tests for those behaviors.

## Requirements Checklist

- Preserve explicit constructor `database_url` and `api_base_url` values when
  service resolution receives a custom `environ` mapping with port overrides.
- Stop storing `_awf_init_fields` through Pydantic `PrivateAttr` dual storage.
- Default compose env discovery must find `docker/compose/.env` inside the
  current checkout/project root from nested working directories.
- Default compose env discovery must not match `docker/compose/.env` above an
  installed module path when the module path is not inside a recognizable
  project root.
- Keep explicit/absolute env-file paths working as before.

## Implementation Steps

1. Add failing unit tests for constructor-explicit field tracking storage and
   constrained module-path compose env lookup.
2. Replace the frozen-model `PrivateAttr` storage with a non-Pydantic sidecar
   registry keyed by settings instance identity.
3. Limit default compose env ancestor candidates to the current directory itself
   and ancestors up to the first recognizable AWF/project root.
4. Run focused tests, then run formatting/lint/type checks justified by the
   touched Python config files.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  must pass.
- `uv run --python 3.12 --extra dev ruff check src/awf tests/unit/service/test_config.py`
  must pass.
- `uv run --python 3.12 --extra dev mypy src/awf`
  must pass.
