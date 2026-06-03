# Grok File Auth Validation

## Scope

Validated the minimal local fix from `plans/GROK_FILE_AUTH_PLAN.md`: AWF can use host
`~/.grok/auth.json` as the preferred Grok auth source by copying only portable auth
files into each workspace auth directory. Cursor remains API-key based and unchanged.

## Results

- `~/.grok/auth.json` is detected before `XAI_API_KEY` in provider readiness.
- Workspace auth resolution creates a per-workspace writable `/home/agent/.grok`
  mount when host `~/.grok/auth.json` exists.
- Only `auth.json` and optional `config.toml` are copied; host runtime folders such
  as `bin/` and session history are not copied.
- Existing per-workspace Grok auth files are preserved on later resolver runs.
- Grok auth is skipped when `auth.json` is absent, even if host `config.toml`
  exists.
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
uv run --python 3.12 --extra dev ruff check src/awf/node/auth_mounts.py src/awf/service/provider_readiness.py tests/unit/node/test_service_auth_mounts.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py
```

Result: `All checks passed`.

```bash
uv run --python 3.12 --extra dev mypy src/awf/node/auth_mounts.py src/awf/service/provider_readiness.py
```

Result: `Success: no issues found in 2 source files`.
