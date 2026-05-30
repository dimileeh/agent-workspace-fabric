# T04 `awf setup --dry-run` System Checks And Readiness Payload — Implementation Plan

## Context

T04 turns the reserved `awf setup` placeholder into the one-time machine setup
wizard *shell*. It runs local-first, bounded host system checks and emits a
reason-coded readiness payload (pretty + json) **without starting Core and
without writing secrets**. It depends on T01 (CLI grammar), T02 (host setup
config + source-checkout asset model), and T03 (first-run error contract +
rendering helpers), all merged on `development`.

The authoritative planning artifact is
`docs/awf-plans/ws_04dcade26c1b4caba84bc5bb.md`; this file mirrors it for the
`plans/` protocol (`AGENTS.md`, `plans/PLAN_EXECUTION_PROTOCOL.md`).

## Goal

Deliver `awf setup --dry-run` plus the flag surface later slices build on
(`--provider` repeatable, `--dry-run`, `--non-interactive`,
`--allow-plain-secrets`, `--source-checkout PATH`, `--format json|pretty`).
Non-dry-run, non-blocked runs may persist **safe, non-secret config only**.

## Scope (in)

- `host_setup/system_checks.py` (new): pure, dependency-injected host readiness
  checks (`SystemCheck`, `SystemChecksReport`, `run_system_checks`).
- `host_setup/rendering.py` (extend): `first_run_setup_readiness_payload(...)`
  builder turning a report + provider/consent/source context into a
  `FirstRunPayload`.
- `cli/setup_commands.py` (rewrite): real wizard shell — flag parse, provider
  validation, source-checkout validation, interactive guard, system-check
  dispatch, readiness rendering, safe config write (non-dry-run only), exit
  codes.
- `cli/main.py` (help text), `host_setup/__init__.py` (exports).

## Out of scope (owned elsewhere)

- Credential storage backends (T06): T04 only forwards `--allow-plain-secrets`.
- Provider setup orchestration / real auth probing (T07): T04 only validates and
  forwards the `--provider` selector.
- Client config writers / `--client` (T08).
- `awf start` wrapper (T05) — referenced only as the next command.
- New reason codes / `docs/REASON_CATALOG.md` edits (already present from T03).
- Starting Core / `awf service bootstrap` of any kind.

## Checks (local-first, bounded; no Core start)

| id | probe | severity | reason |
| --- | --- | --- | --- |
| docker_cli | `which("docker")` | fail if missing | `DOCKER_CLI_NOT_FOUND` |
| docker_daemon | `docker info --format {{json .}}` (5s) | fail if non-zero/timeout/unreachable | `DOCKER_DAEMON_UNREACHABLE` |
| docker_compose | `docker compose version --short` (5s) | fail if missing/non-zero | `COMPOSE_NOT_AVAILABLE` (report-local) |
| git | `which("git")` | fail if missing | `GIT_NOT_FOUND` (report-local) |
| gh | `which("gh")` | warn only | `GH_NOT_FOUND` (report-local) |
| python_runtime | `version >= (3, 12)` | fail if below | `PYTHON_RUNTIME_BELOW_FLOOR` (report-local) |
| port_api | connect (host, api_port) | warn if in use; ok if refused | report-local |
| port_db | connect (host, 5432) | warn if in use; ok if refused | report-local |
| disk | `check_disk_space(work_dir, ...)` | fail if insufficient | `INSUFFICIENT_DISK`/`SUFFICIENT_DISK` |
| shell_path | `which("awf")` | warn if missing | `AWF_NOT_ON_PATH` (report-local) |
| capacity | parse NCPU/MemTotal from the same `docker info` JSON | never blocker (warn if unknown) | report-local |

Notes: capacity reuses the single `docker info` call; report-local reasons live
only in the structured report (never via `error_code=` and never added to
`FIRST_RUN_*` tuples), so `test_catalog_coverage` is not tripped. Subprocess and
socket failures catch specific exceptions
(`FileNotFoundError`, `OSError`, `subprocess.TimeoutExpired`,
`json.JSONDecodeError`) → `fail`/`warn`, never bare `except`. Metadata is
secret-free by construction.

## Readiness payload builder

`first_run_setup_readiness_payload(report, *, command="awf setup", dry_run,
providers, allow_plain_secrets, source_checkout_root)`:

- details: `dry_run`, `providers`, `provider_scope` (`all` vs `targeted recheck`),
  `allow_plain_secrets`, optional `source_checkout`, optional `capacity`,
  `checks` (per-check id/status/reason/message/fix/docs_link), `blockers`,
  `warnings`.
- status mapping: `fail` → `first_run_failure_payload(SETUP_READINESS_FAILED,
  status="blocked")` with next_steps = deduped blocker fixes + re-run; `warn` →
  success payload carrying a warning-severity note + warnings in details (exit
  0); `ok` → `first_run_success_payload`. Success/warn next command = `awf start`.
- redaction is inherited from `render_first_run_json`.

## CLI control flow + exit codes

1. Provider validation (normalize/dedup, order-stable; set =
   `github, codex, claude_code, gemini, opencode`; `claude` → `claude_code`).
   Unknown → `SETUP_PROVIDER_UNKNOWN`, **exit 2**, no system checks run.
2. `read_host_setup_config()`; `HostSetupConfigError` → `exc.reason_code` failure,
   exit 1.
3. `--source-checkout` → `validate_source_checkout`; `SourceCheckoutError` →
   `exc.reason_code` (+ missing_markers), exit 1.
4. Interactive guard: `--non-interactive` + `--provider` + not `--dry-run` →
   `INTERACTIVE_INPUT_REQUIRED`, exit 1.
5. `run_system_checks(host_port=config.api.host_port, work_dir=config.work_dir)`.
6. Build readiness payload.
7. Safe config write only when not `--dry-run` and not blocked (consent flags +
   verified source metadata; never secrets).
8. Emit: JSON → stdout; pretty success/warn → stdout exit 0; pretty
   failure/blocked → stderr exit 1.

## TDD test plan

- `tests/unit/service/test_host_setup_system_checks.py` (new): healthy pass,
  docker cli/daemon/compose blockers, git blocker, gh warning, python floor,
  port in-use warning / free ok, disk insufficient blocker, capacity warn-only,
  shell_path warn, status rollup, subprocess timeout handled, no-Core-start
  command allowlist, metadata secret-free.
- `tests/unit/service/test_host_setup_rendering.py` (extend): readiness success,
  blocked (blockers + docs links + next_steps), warning-succeeds, redaction.
- `tests/unit/cli/test_setup_commands.py` (rewrite): help, dry-run success,
  dry-run no write, docker blocker readiness failure, provider forwarded /
  deduped / multiple, unknown provider exit 2, allow-plain-secrets forwarded,
  source-checkout valid / invalid, non-interactive provider requires input,
  non-dry-run writes safe config, config corrupt surfaces reason, json/pretty
  shapes.
- Keep `tests/unit/cli/test_first_run_command_imports.py` green.

## Validation commands

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/cli/test_first_run_command_imports.py \
  tests/unit/service/test_host_setup_system_checks.py \
  tests/unit/service/test_host_setup_rendering.py \
  tests/unit/docs/test_catalog_coverage.py \
  tests/unit/service/test_doctor_reasons.py -q
```

Broad coverage/CI validation is owned by AWF/GitHub after the agent phase.
