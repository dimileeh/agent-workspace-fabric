# AWF T08 — Claude/Codex Client Config Helpers (Plan)

This is the task-specific plan required by `AGENTS.md` /
`plans/PLAN_EXECUTION_PROTOCOL.md`. The authoritative, detailed implementation
contract for this workspace is `docs/awf-plans/ws_526fa38093c44e5196d06dda.md`;
this file summarizes the execution and records the focused validation lane.

## Goal

Add client-integration helpers that register AWF's local stdio MCP server into
the **Claude Code** (`~/.claude.json`, JSON) and **Codex** (`~/.codex/config.toml`,
TOML) client config, plus the narrow `awf setup --client …` dispatch after T04.

Helpers must: prefer the official client CLI when present, fall back to
structured JSON/TOML parsing, show a unified diff, write a timestamped backup
before mutating, detect/refuse ambiguous conflicts, support `--dry-run`,
preserve unrelated existing config, and never read/accept/store/return provider
tokens (only an env-file **path** is threaded through, never its contents).

## Steps

1. `src/awf/host_setup/rendering.py`: add `SETUP_CLIENT_UNKNOWN` reason code to
   `FIRST_RUN_CLIENT_REASON_CODES` and `__all__`.
2. `src/awf/service/doctor/reasons.py`: add a `_ReasonText` catalog entry for
   `SETUP_CLIENT_UNKNOWN`; regenerate `docs/REASON_CATALOG.md`.
3. `src/awf/host_setup/clients.py` (new): `normalize_client(s)`,
   `ClientDescriptor` + `CLIENT_DESCRIPTORS`, `ClientConfigPlan`,
   `ClientWriteResult`, `build_client_config_plan`, `apply_client_config_plan`,
   `setup_client`. A scoped, deterministic TOML emitter (string/int/bool/array
   only) re-emits Codex config; unrepresentable existing TOML → `conflict`.
4. `src/awf/host_setup/__init__.py`: re-export the new public names +
   `SETUP_CLIENT_UNKNOWN`.
5. `src/awf/cli/setup_commands.py`: add repeatable `--client`; dispatch to a
   focused client branch before the readiness path, leaving the no-`--client`
   path byte-for-byte unchanged.
6. `docs/MCP_SETUP.md`: narrow `--client` note (broad docs are T15).
7. Tests: `tests/unit/service/test_host_setup_clients.py` (new),
   extend `tests/unit/cli/test_setup_commands.py`.

## Conflict semantics (precise)

An existing `awf`-keyed server entry whose **command/args** differ from the
desired entry → `conflict` (refuse). Identical command/args → `no_change`.
Absent entry → `create` (no prior file) / `update` (file exists with other
content). Malformed/unparseable existing config, or Codex TOML that the scoped
emitter cannot round-trip → `conflict` (ambiguous; refuse).

## Focused validation (AWF/CI owns the broad gate)

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev ruff format --check src/awf/host_setup/clients.py src/awf/cli/setup_commands.py
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_clients.py tests/unit/cli/test_setup_commands.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py tests/unit/service/test_host_setup_rendering.py tests/unit/docs/test_catalog_coverage.py -q
uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py   # regen, confirm no drift
```
