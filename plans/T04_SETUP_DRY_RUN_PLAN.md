# T04 — `awf setup --dry-run` system checks and readiness payload (PLAN)

Protocol plan per `plans/PLAN_EXECUTION_PROTOCOL.md`. This mirrors the AWF
planning artifact `docs/awf-plans/ws_4c144afc35444a9bbf88e5c6.md`; that artifact
holds the full reuse map and design notes. Validation lives in
`plans/T04_SETUP_DRY_RUN_VALIDATION.md`.

## 1. Problem statement and scope

Backlog task **T04** (`TODO/awf-full-installer-first-run-setup-backlog.md`).
T01 registered `awf setup` as a placeholder Typer command (`AWF_SETUP_PLACEHOLDER`,
exit 1). T04 replaces that placeholder with the first real capability of the
one-time machine setup wizard: a **read-only host system-readiness pass** that
verifies the machine can run AWF Core **without starting Core and without writing
any secret**.

In scope:

- Real `setup_command` option surface + dispatch: `--provider PROVIDER`
  (repeatable), `--dry-run/--no-dry-run`, `--non-interactive`,
  `--allow-plain-secrets`, `--source-checkout PATH`, `--format json|pretty`.
- New stdlib-only `src/awf/host_setup/system_checks.py`: Docker (CLI + daemon),
  Compose, Git, `gh`, Python/runtime, ports, disk, shell/PATH, local capacity —
  all read-only, no Core start.
- Aggregate checks into a first-run readiness payload, rendered through the T03
  layer (status, blockers, warnings, docs links, next command) in `json` and
  `pretty`.
- Safe, non-secret config writes only when **not** dry-run (reuse T02
  `HostSetupConfig` + `write_host_setup_config`); never write in dry-run.
- Validate and **forward** (not consume) the `--provider` selector and
  `--allow-plain-secrets` consent for T06/T07.

Out of scope (owned elsewhere): credential storage (T06), provider
auth/orchestration (T07), client config writers (T08), `awf start` (T05), MCP
tools (T09), packaging/installer/docs/release (T11–T18), reason-catalog edits.

## 2. Requirements checklist (acceptance criteria)

- A1: `awf setup --dry-run` never writes secrets and never starts Core.
- A2: `awf setup --provider github --dry-run` accepts and forwards the selector.
- A3: `awf setup --allow-plain-secrets` accepts/forwards the plain-file consent
  gate without making plain-file the default.
- A4: Unknown provider names fail with a reason-coded diagnostic
  (`SETUP_PROVIDER_UNKNOWN`), never silent all-provider fallback.
- A5: Missing Docker or stopped daemon returns a readiness failure
  (`SETUP_READINESS_FAILED`) with next actions and exit 1.
- A6: Source-checkout dry-run works from a cloned AWF checkout and from
  `uv run … awf setup --source-checkout .`; invalid → `SOURCE_CHECKOUT_INVALID`.
- A7: Pretty output includes status, blockers, warnings, docs links, next command.
- A8: Non-interactive path needing input returns `INTERACTIVE_INPUT_REQUIRED`.

## 3. Implementation steps

1. `system_checks.py` (stdlib only): `SetupCheckLevel`, `SetupCheckResult`,
   `SetupCheckError`; `check_docker/compose/git/gh/python_runtime/ports/disk/
   shell_path/local_capacity`; `run_system_checks`; `KNOWN_SETUP_PROVIDERS` +
   `normalize_provider`/`normalize_providers`; `require_interactive`;
   `build_setup_readiness_payload`. Every subprocess probe bounded with a hard
   timeout and specific exception handling (no bare `except`, no hidden retry).
2. `rendering.py`: add generic `first_run_report_payload(*, command, summary,
   issues, details, next_steps)` deriving top-level `status`/`reason_code` from
   issue severities. Export from `host_setup/__init__.py`.
3. `setup_commands.py`: replace placeholder with the option surface and flow —
   normalize providers → read config → validate source checkout → run checks →
   build payload → (not dry-run) safe config write with interactive guard →
   render → exit code.
4. `main.py`: update `awf setup` help text to describe the readiness pass; keep a
   stable "Prepare this machine for AWF" fragment.
5. Tests first (TDD): system-check fixtures, rendering helper, CLI behavior/edge.

## 4. Reason codes (all reused; catalog untouched)

`SETUP_READINESS_FAILED`, `SETUP_PROVIDER_UNKNOWN`, `INTERACTIVE_INPUT_REQUIRED`
(T03), `SOURCE_CHECKOUT_INVALID` (T02). Per-check problem/cause/fix/docs come
from `first_run_issue_from_reason_code` overrides.

## 5. Verification commands and pass criteria

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/service/test_host_setup_system_checks.py \
  tests/unit/service/test_host_setup_rendering.py \
  tests/unit/service/test_host_setup_config.py -q
```

Pass criteria: lint/type clean for the touched scope; the focused pytest subset
green; behavior + edge + error coverage for A1–A8; owned test path
`tests/unit/service/test_host_setup_system_checks.py` exists. Broad/CI coverage
gates are owned by AWF/GitHub after the agent phase.

## 6. Assumptions / Changes

- T01/T02/T03 are merged on `development` and behave as read here.
- `host_setup` stays dependency-light; `system_checks` imports stdlib only.
- Docker may be absent in the workspace; all tests are hermetic (injected
  fakes), no real Docker/network/process/Core start.
- Interactive trigger chosen for T04: in **non-dry-run** mode, supplying a
  `--provider` selector under `--non-interactive` raises
  `INTERACTIVE_INPUT_REQUIRED`, because configuring the selected provider needs
  interactive credential entry that T04 cannot collect (T07 owns the real
  orchestration). Dry-run skips this step entirely, so dry-run never triggers it.
