# AWF T08 — Claude/Codex Client Config Helpers (Validation)

Validation record for the T08 implementation. The authoritative implementation
contract is `docs/awf-plans/ws_526fa38093c44e5196d06dda.md`; the task plan is
`plans/AWF_T08_CLIENT_CONFIG_HELPERS_PLAN.md`.

Broad AWF/GitHub CI validation (the full unit suite, the 99% repo-wide coverage
gate, and the OpenAPI/reason-catalog drift gates) runs after the agent phase and
is **not** executed here; the commands below are the focused, scoped checks the
workspace contract permits.

## What was implemented

- New `src/awf/host_setup/clients.py`:
  - `normalize_client` / `normalize_clients` (alias map + `SETUP_CLIENT_UNKNOWN`).
  - `ClientDescriptor` + `CLIENT_DESCRIPTORS` for Claude Code (`~/.claude.json`,
    JSON, `mcpServers`) and Codex (`~/.codex/config.toml`, TOML, `mcp_servers`).
  - `build_client_config_plan` (pure read+compute, structured parse, unified
    diff, conflict detection, method selection) and `apply_client_config_plan`
    (official-CLI preferred, else timestamped backup + atomic `0o600` write).
  - `setup_client` orchestrator returning reason-coded `FirstRunPayload`s.
  - A scoped, deterministic TOML emitter (string/int/bool/array only); existing
    TOML it cannot round-trip becomes a refused `conflict`.
- New reason code `SETUP_CLIENT_UNKNOWN` in `rendering.py`,
  `service/doctor/reasons.py`, `host_setup/__init__.py`, and a regenerated
  `docs/REASON_CATALOG.md`.
- `src/awf/cli/setup_commands.py`: additive repeatable `--client` option +
  dispatch branch (returns before the readiness path; no-`--client` flow
  unchanged); injectable home/which/run/now seams.
- `docs/MCP_SETUP.md`: a narrow assisted-`--client` note.
- Tests: new `tests/unit/service/test_host_setup_clients.py`; extended
  `tests/unit/cli/test_setup_commands.py`.

## Acceptance criteria — evidence

- **`awf setup --client claude|codex` can produce a dry-run diff** — covered by
  `test_setup_client_dry_run_emits_diff_without_writing` (CLI) and the
  `build_plan_*_missing_file_creates` module tests.
- **Config write creates a backup and refuses ambiguous conflicts** —
  `test_setup_client_apply_writes_config_and_backup`,
  `test_apply_file_update_backs_up_and_preserves`,
  `test_setup_client_conflict_exits_nonzero_without_mutation`,
  malformed JSON/TOML and unrepresentable-TOML conflict tests.
- **Existing unrelated client config is preserved** —
  `test_apply_file_update_backs_up_and_preserves`,
  `test_build_plan_codex_preserves_unrelated_tables_on_update`.
- **Client setup never reads/accepts/stores/returns provider tokens** —
  `test_setup_client_redacts_token_shaped_env_file_path`,
  `test_build_plan_never_reads_env_file_contents`.

## Focused commands run (all green)

```text
uv run --python 3.12 --extra dev ruff check src/awf tests                 # All checks passed!
uv run --python 3.12 --extra dev ruff format --check <changed files>       # already formatted
uv run --python 3.12 --extra dev mypy                                      # Success: no issues found in 327 source files
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_host_setup_clients.py \
  tests/unit/cli/test_setup_commands.py -q                                 # 103 passed
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_doctor_reasons.py \
  tests/unit/service/test_host_setup_rendering.py \
  tests/unit/docs/test_catalog_coverage.py -q                              # passed
uv run --python 3.12 --extra dev python scripts/generate_reason_catalog.py # regenerated; drift test green
```

Focused coverage of the two changed modules
(`--cov=awf.host_setup.clients --cov=awf.cli.setup_commands`) reports **100%**;
genuinely defensive/platform branches carry justified `# pragma: no cover`.

## Out of scope (per plan boundaries)

- No MCP setup tools (T09), no provider orchestration (T07), no broad docs
  rewrite (T15), no `awf start`/bootstrap changes, no new TOML/JSON dependency.
