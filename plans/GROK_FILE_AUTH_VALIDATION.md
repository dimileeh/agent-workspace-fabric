# Grok File Auth Validation

## Scope

Validated the minimal local fix from `plans/GROK_FILE_AUTH_PLAN.md`: AWF can use host
`~/.grok/auth.json` as the preferred Grok auth source by copying only portable auth
files into each workspace auth directory. Cursor remains API-key based and unchanged.

## Results

- `~/.grok/auth.json` is detected before `XAI_API_KEY` in provider readiness.
- Local service API/worker containers declare the read-only host `~/.grok`
  mount, allowing Docker service mode to see the same auth source as local
  readiness checks.
- Workspace auth resolution creates a per-workspace writable `/home/agent/.grok`
  mount when host `~/.grok/auth.json` exists.
- Only `auth.json` and optional `config.toml` are copied; host runtime folders such
  as `bin/` and session history are not copied.
- Existing per-workspace Grok auth files are preserved on later resolver runs.
- Grok auth is skipped when `auth.json` is absent, even if host `config.toml`
  exists.
- The authenticated Grok CLI in the runtime reports `grok-build` as the default
  model, and AWF defaults use that model rather than the rejected
  `grok-build-0.1` ID.
- Cursor auth behavior was not changed.

## Validation Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_service_auth_mounts.py::test_service_auth_mounts_include_existing_host_credentials tests/unit/node/test_service_auth_mounts.py::test_service_auth_mounts_skip_grok_when_auth_json_missing tests/unit/node/test_service_auth_mounts.py::test_service_auth_mounts_preserve_existing_workspace_grok_auth tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py::test_selected_grok_preflight_uses_file_auth_before_xai_api_key tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py::test_provider_readiness_grok_file_present_before_env -q
```

Result: `5 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/node/test_service_auth_mounts.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q
```

Result: `126 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/integration/test_local_service_compose.py::test_local_service_compose_declares_control_plane_stack -q
```

Result: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter tests/unit/adapters/test_adapters.py::TestCentralDefaults tests/unit/adapters/test_provider_failures.py tests/unit/service/test_provider_recovery_coverage_gaps.py -q
```

Result: `63 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_cli_parts/test_cli_part_001.py::TestWorkspaceCreate::test_workspace_create_accepts_grok_agent tests/unit/cli/test_cli_parts/test_cli_part_002.py::TestWorkspaceAdoptPr::test_posts_grok_agent_when_adopting_pr -q
```

Result: `2 passed`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/adapters/defaults.py src/awf/adapters/grok.py src/awf/node/auth_mounts.py src/awf/service/provider_readiness.py tests/unit/adapters/test_adapters.py tests/unit/adapters/test_provider_failures.py tests/unit/service/test_provider_recovery_coverage_gaps.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/node/test_service_auth_mounts.py tests/unit/cli/test_cli_parts/test_cli_part_001.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/integration/test_local_service_compose.py
```

Result: `All checks passed`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/adapters/defaults.py src/awf/adapters/grok.py src/awf/node/auth_mounts.py src/awf/service/provider_readiness.py
```

Result: `Success: no issues found in 4 source files`.

```bash
docker compose --env-file docker/compose/.env -f docker/compose/local-service.yml config --quiet
```

Result: passed with no output.
