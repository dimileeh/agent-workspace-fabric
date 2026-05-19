# PRRT_kwDOSJAM6s6DRZFy Compose DB URL Explicitness Plan

## Problem Statement And Scope

Review thread `PRRT_kwDOSJAM6s6DRZFy` reports that
`resolve_service_settings()` decides whether `AWF_DATABASE_URL` is explicit
from the host environment alone. When a developer has sourced a project `.env`
that exports the stock local database URL, but `docker/compose/.env` supplies a
different `AWF_POSTGRES_HOST_PORT`, host-side status/doctor/readiness can keep
probing the stale default port.

Scope is limited to local service database URL explicitness. Existing behavior
that treats a manually supplied host default URL as explicit must remain intact.

## Requirements Checklist

- [ ] Add a failing unit test for a sourced project `.env` default
  `AWF_DATABASE_URL` plus compose-only `AWF_POSTGRES_HOST_PORT`.
- [ ] Use the merged service environment when deciding whether the default
  host database URL can be derived from the Compose Postgres port.
- [ ] Preserve existing explicit host/default and constructor/default
  regression tests.
- [ ] Run focused service config tests and lint for touched files.

## Implementation Steps

1. Add regression coverage in `tests/unit/service/test_config.py`.
2. Confirm the new regression fails against the current implementation.
3. Update `src/awf/service/config.py` so a host-exported stock database URL
   matching the project `.env` default is derivable when the merged Compose
   environment supplies `AWF_POSTGRES_HOST_PORT`.
4. Re-run the focused regression group, then the service config test file.
5. Write validation evidence in
   `plans/PRRT_kwDOSJAM6s6DRZFy_VALIDATION.md`.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py::test_service_settings_sourced_env_default_database_url_uses_compose_env_postgres_host_port -q`
  fails before implementation and passes after implementation.
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/service/config.py tests/unit/service/test_config.py`
  passes.
