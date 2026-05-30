# T04 — `awf setup --dry-run` System Checks And Readiness Payload Plan

## Planning Context

This planning artifact is constrained to
`docs/awf-plans/ws_04dcade26c1b4caba84bc5bb.md` by the AWF planning-phase prompt.
No implementation files are modified during planning.

Per `AGENTS.md` and `plans/PLAN_EXECUTION_PROTOCOL.md`, the **implementation
phase** must additionally create:

- `plans/T04_SETUP_DRY_RUN_PLAN.md` (mirror of this plan) before coding, and
- `plans/T04_SETUP_DRY_RUN_VALIDATION.md` after implementation
  (requirement-by-requirement `Complete | Partial | Missing` with evidence).

Source inputs reviewed:

- `TODO/awf-full-installer-first-run-setup-backlog.md` (task **T04** card,
  conflict flags, critical failure-mode table).
- T01 wiring: `src/awf/cli/main.py` (registers `setup`/`start` commands),
  `src/awf/cli/setup_commands.py` (current placeholder).
- T02 config/asset model: `src/awf/host_setup/config.py`,
  `src/awf/host_setup/source_assets.py`.
- T03 error contract + rendering: `src/awf/host_setup/rendering.py`,
  `src/awf/service/doctor/reasons.py`, `docs/REASON_CATALOG.md`,
  `tests/unit/service/test_host_setup_rendering.py`,
  `tests/unit/cli/test_setup_commands.py`, `tests/unit/cli/test_first_run_command_imports.py`.
- Reusable system probes: `src/awf/service/doctor/__init__.py` (Docker/port/socket
  probes, redaction), `src/awf/service/disk.py` (`check_disk_space`),
  `src/awf/service/local_capacity.py` (`docker info` capacity parse),
  `src/awf/service/provider_readiness.py` (`PROVIDER_NAMES`),
  `src/awf/common/config.py` (`DEFAULT_MIN_FREE_DISK_BYTES`).

T01, T02, and T03 are treated as **merged and available**; their behavior is
reused, not reimplemented. The reason codes T04 needs already exist in
`_REASON_TEXT` and `docs/REASON_CATALOG.md`
(`SETUP_READINESS_FAILED`, `SETUP_PROVIDER_UNKNOWN`, `INTERACTIVE_INPUT_REQUIRED`,
`SOURCE_CHECKOUT_INVALID`, `HOST_SETUP_CONFIG_*`, `DOCKER_CLI_NOT_FOUND`,
`DOCKER_DAEMON_UNREACHABLE`, `INSUFFICIENT_DISK`), so **no new reason codes and
no `docs/REASON_CATALOG.md` edits are expected**.

## Objective

Implement only **T04**: turn the reserved `awf setup` placeholder into the
one-time machine setup wizard *shell* that runs **local-first, bounded host
system checks** and emits a reason-coded **readiness payload** in `pretty` and
`json` form, **without starting Core and without writing secrets**.

The deliverable is `awf setup --dry-run` plus the parser/flag surface that later
slices (T06 credentials, T07 provider orchestration, T08 client config) build
on. Non-dry-run runs may write **safe, non-secret config only**.

## Scope (in)

- `awf setup` command shell with these flags:
  - `--provider PROVIDER` (repeatable), `--dry-run`, `--non-interactive`,
    `--allow-plain-secrets`, `--source-checkout PATH`, `--format json|pretty`.
- Host system checks (no Core start): Docker CLI, Docker daemon, Docker Compose,
  Git, `gh`, Python/runtime floor, ports (API host port + Postgres), disk,
  shell/PATH reachability, and local capacity.
- A structured readiness payload built from the T03 `FirstRunPayload` contract.
- Safe config persistence (non-secret only) when **not** in `--dry-run`.

## Out of scope / Non-goals (owned elsewhere)

- Credential storage backends (keyring/env/plain-file) — **T06**. T04 only
  *accepts and forwards* the `--allow-plain-secrets` consent flag; it never
  stores a secret and never makes plain-file the default.
- Provider setup orchestration / probing real provider auth — **T07**. T04 only
  *validates and forwards* the `--provider` selector as a recheck scope.
- Claude/Codex client config writers and `--client` flag — **T08**.
- `awf start` startup wrapper — **T05** (T04 only references it as the next
  command).
- New reason codes or `docs/REASON_CATALOG.md` changes (already present from T03).
- Starting Core / `awf service bootstrap` invocation of any kind.

## Intended Files And Modules To Touch (implementation phase)

Process artifacts:

- `plans/T04_SETUP_DRY_RUN_PLAN.md` (new)
- `plans/T04_SETUP_DRY_RUN_VALIDATION.md` (new, post-implementation)

New runtime module:

- `src/awf/host_setup/system_checks.py` (new)
  - Pure, dependency-injected host readiness checks returning structured results.

Edited runtime modules:

- `src/awf/host_setup/rendering.py`
  - Add a readiness payload builder that turns a system-checks report (plus
    provider/consent/source context) into a `FirstRunPayload`. Keeps rendering
    integration in the T03 module per the backlog "Modules touched" list.
- `src/awf/cli/setup_commands.py`
  - Replace the placeholder with the real wizard shell: flag parsing, provider
    selector validation, source-checkout validation, system-check dispatch,
    readiness rendering, safe config write (non-dry-run only), exit codes.
- `src/awf/cli/main.py`
  - Update the registered `setup` command `help=` string to drop "Reserved
    before full setup checks land" and describe the real flags. (Small, scoped;
    the `start` command help is left untouched — that is T05.)
- `src/awf/host_setup/__init__.py`
  - Export the new `system_checks` symbols and the new rendering builder, and add
    them to `__all__`.

Tests (written first — see TDD section):

- `tests/unit/cli/test_setup_commands.py` (rewritten for real behavior)
- `tests/unit/service/test_host_setup_system_checks.py` (new)
- `tests/unit/service/test_host_setup_rendering.py` (extended for the builder)

Existing `tests/unit/cli/test_first_run_command_imports.py` must keep passing
(no payload construction at import time — all payload building stays inside
functions).

## Design

### 1. `host_setup/system_checks.py` (new)

Provider-neutral, **local-first and bounded** host checks. No Core startup, no
network beyond bounded local socket probes and bounded `docker` subprocess calls.

Result types (frozen `pydantic` models, mirroring the T03 strict/immutable base
or simple frozen dataclasses — match the surrounding `host_setup` pydantic
style):

- `SystemCheck`: `id`, `label`, `status` (`ok|warn|fail|skipped`), `reason`
  (stable string), `message`, `fix`, `docs_link`, `metadata` (secret-free).
- `SystemChecksReport`: `checks: tuple[SystemCheck, ...]`, derived
  `status` (`ok|warn|fail`), `blockers` (fail checks), `warnings` (warn checks),
  optional `capacity` metadata. Includes `to_dict()` for JSON/detail embedding.

Orchestrator:

```text
run_system_checks(
    *,
    host_port: int,                 # from HostSetupConfig.api.host_port
    work_dir: str,                  # from HostSetupConfig.work_dir (expanduser'd)
    min_free_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES,
    # injectable probes (real defaults) for deterministic tests:
    which: Callable[[str], str | None] = shutil.which,
    run_subprocess: SubprocessRun = <bounded subprocess.run>,
    socket_connector: SocketConnector = <socket.create_connection>,
    disk_usage: DiskUsage | None = None,           # -> check_disk_space
    environ: Mapping[str, str] = os.environ,
    python_version: tuple[int, int] = sys.version_info[:2],
    docker_host: str | None = None,
) -> SystemChecksReport
```

Check inventory and severity:

| id | probe | blocker? | reused reason / source |
| --- | --- | --- | --- |
| `docker_cli` | `which("docker")` | **fail** if missing | `DOCKER_CLI_NOT_FOUND` |
| `docker_daemon` | `docker info --format {{json .}}` (bounded) | **fail** if non-zero / timeout / unreachable | `DOCKER_DAEMON_UNREACHABLE` |
| `docker_compose` | `docker compose version --short` (bounded) | **fail** if missing/non-zero | setup-local reason `COMPOSE_*` in report detail |
| `git` | `which("git")` | **fail** if missing | setup-local reason in report detail |
| `gh` | `which("gh")` | warn only (env ref token is an alternative; GitHub is T07) | setup-local reason |
| `python_runtime` | `python_version >= (3, 12)` | **fail** if below floor | setup-local reason |
| `port_api` | `socket_connector((host, host_port))` | warn if **in use** (may be a running AWF); ok if refused | setup-local reason |
| `port_db` | `socket_connector((host, 5432))` | warn if in use; ok if refused | setup-local reason |
| `disk` | `check_disk_space(work_dir, min_free_bytes=...)` | **fail** if insufficient | `INSUFFICIENT_DISK` / `SUFFICIENT_DISK` |
| `shell_path` | `which("awf")` | warn if not on PATH (PATH hint in fix) | setup-local reason |
| `capacity` | parse `NCPU`/`MemTotal` from the **same** `docker info` JSON | warn if unknown/low; never blocker | setup-local reason |

Notes:

- **Capacity reuses the single `docker info` call** from `docker_daemon` (parse
  `NCPU`/`MemTotal` like `local_capacity._positive_float`/`_bytes_to_gib`), so we
  avoid a second subprocess and avoid constructing full `Settings`.
- Per-check `reason` strings that are *not* in the doctor catalog are carried
  only inside the structured report (NOT via `error_code=` and NOT through
  `first_run_issue_from_reason_code`), so the `test_catalog_coverage` guard is not
  tripped. The **top-level** payload reason code is always a documented one.
- All subprocess calls use a bounded timeout (~5s, matching doctor); timeouts and
  `FileNotFoundError`/`OSError` are caught specifically and converted to `fail`
  results — never bare `except Exception` swallowing, per AGENTS.md.
- `metadata` is kept secret-free by construction (versions, ports, byte counts,
  booleans only); no env values are copied in.

### 2. `host_setup/rendering.py` (extend)

Add a builder, e.g. `first_run_setup_readiness_payload(...)`:

```text
first_run_setup_readiness_payload(
    report: SystemChecksReport,
    *,
    command: str = "awf setup",
    dry_run: bool,
    providers: tuple[str, ...],          # validated recheck scope ([] = all)
    allow_plain_secrets: bool,
    source_checkout_root: str | None,
) -> FirstRunPayload
```

- `details` mapping (JSON-safe, redacted by existing `render_first_run_json`):
  `dry_run`, `providers`, `provider_scope` (`"all"` vs `"targeted recheck"`),
  `allow_plain_secrets`, `source_checkout`, `capacity`, and a `checks` list with
  per-check `id/status/reason/message/fix/docs_link`, plus `blockers`/`warnings`.
- Status mapping → payload:
  - `report.status == "fail"` → `first_run_failure_payload(reason_code=
    SETUP_READINESS_FAILED, status="blocked", details=..., next_steps=...)`.
  - `report.status == "warn"` → `first_run_warning_payload(reason_code=
    SETUP_READINESS_FAILED, ...)` **or** a success-with-warnings payload; chosen
    so warnings render but the command still succeeds (exit 0). (Decision: warn →
    success payload carrying warnings in details + a `warning`-severity note, so
    non-blocking advisories do not fail the command. Finalized in tests.)
  - `report.status == "ok"` → `first_run_success_payload(...)`.
- `next_steps`: on success/warn → the next command (`awf start`); on failure →
  the de-duplicated blocker `fix` actions followed by
  `Re-run awf setup --dry-run`. Satisfies "next actions" for Docker failures and
  "status, blockers, warnings, docs links, and next command" in pretty output.
- Reuses existing redaction (`render_first_run_json` →
  `redact_first_run_value`), so provider refs / token-shaped values never render.

This keeps `rendering.py` one-directional: it may import `system_checks`;
`system_checks` must **not** import `rendering` (no cycle).

### 3. `cli/setup_commands.py` (rewrite)

`setup_command` signature (Typer), mirroring the repeatable-option pattern
already used by `init` (`provider: list[str] = typer.Option([], "--provider")`):

```text
setup_command(
    provider: list[str]      = Option([], "--provider"),     # repeatable
    dry_run: bool            = Option(False, "--dry-run"),
    non_interactive: bool    = Option(False, "--non-interactive"),
    allow_plain_secrets: bool= Option(False, "--allow-plain-secrets"),
    source_checkout: Path|None = Option(None, "--source-checkout"),
    fmt: OutputFormat        = Option(OutputFormat.pretty, "--format"),
)
```

Control flow:

1. **Provider validation** — normalize + de-duplicate selectors (order-stable),
   validate against the known setup-provider set
   (`github, codex, claude_code, gemini, opencode`; `docker` is a *system check*,
   not a provider). Unknown → emit `SETUP_PROVIDER_UNKNOWN` failure, **exit 2**
   (usage error, consistent with `_emit_init_migration_error`). No system checks
   run on this path → "no silent fallback to all-provider setup".
2. **Config read** — `read_host_setup_config()`; on `HostSetupConfigError`
   surface `exc.reason_code` (`HOST_SETUP_CONFIG_CORRUPT|SECRET_VALUE`) failure.
3. **Source-checkout** — if `--source-checkout` given, `validate_source_checkout`;
   on `SourceCheckoutError` emit failure with `exc.reason_code`
   (`SOURCE_CHECKOUT_INVALID`) + `missing_markers`, **exit 1**.
4. **Interactive guard** — if `--non-interactive` **and** a `--provider` is
   selected **and** not `--dry-run` (i.e. setup would begin provider
   configuration that needs prompt input deferred to T06/T07), emit
   `INTERACTIVE_INPUT_REQUIRED`, **exit 1**. (See assumption A4.)
5. **System checks** — `run_system_checks(host_port=config.api.host_port,
   work_dir=config.work_dir, ...)`.
6. **Build payload** — `first_run_setup_readiness_payload(report, dry_run=...,
   providers=..., allow_plain_secrets=..., source_checkout_root=...)`.
7. **Safe config write** — **only when not `--dry-run` and not blocked**: merge
   safe, non-secret fields into the existing config and `write_host_setup_config`
   (e.g. record verified `source_checkout` metadata, `consent.plain_file_secrets`
   from `--allow-plain-secrets`, `consent.source_checkout_assets`). Never writes
   secrets; the config schema already rejects secret payloads.
8. **Emit + exit**:
   - JSON: `_emit(render_first_run_json(payload), fmt)` → stdout (scripting).
   - Pretty success/warn → `typer.echo(render_first_run_pretty(payload))` stdout,
     **exit 0**.
   - Pretty failure/blocked → `typer.echo(..., err=True)` stderr, **exit 1**.

Exit-code contract: `0` ready / warnings-only; `1` readiness blocked, source
invalid, config error, interactive-required; `2` unknown provider (usage).

`main.py`: update `app.command("setup", help=...)` to describe the real command
(keep the "Prepare this machine for AWF" lead used by the help test).

### Reuse map

| Need | Reused from |
| --- | --- |
| Payload models, redaction, pretty/json render | `host_setup.rendering` (T03) |
| Reason text/remediation lookup | `doctor.reasons.reason_text_for_code` via T03 helpers |
| Config read/write, secret rejection, consent fields | `host_setup.config` (T02) |
| Source-checkout validation + reason codes | `host_setup.source_assets` (T02) |
| Disk free-space check | `service.disk.check_disk_space` |
| Docker capacity parse pattern | `service.local_capacity` |
| Provider name vocabulary | `service.provider_readiness.PROVIDER_NAMES` |
| Bounded subprocess / socket protocols | mirror `service.doctor.models` |
| Disk threshold default | `common.config.DEFAULT_MIN_FREE_DISK_BYTES` |

## Tests To Write First (TDD)

Write/adjust these failing tests before implementation. Markers: `@pytest.mark.unit`.

### `tests/unit/service/test_host_setup_system_checks.py` (new — fixture-driven)

Inject fake `which`, `run_subprocess`, `socket_connector`, `disk_usage`,
`environ`, `python_version` so no Docker/network is required.

1. `test_all_checks_pass_with_healthy_fakes` → report `status == "ok"`, empty
   blockers, capacity populated from fake `docker info`.
2. `test_docker_cli_missing_is_blocker` → `which("docker")` None → `fail`,
   reason `DOCKER_CLI_NOT_FOUND`, present in `blockers`.
3. `test_docker_daemon_unreachable_is_blocker` → `docker info` returncode≠0 →
   `fail` `DOCKER_DAEMON_UNREACHABLE`.
4. `test_docker_compose_missing_is_blocker`.
5. `test_git_missing_is_blocker`.
6. `test_gh_missing_is_warning_not_blocker`.
7. `test_python_below_floor_is_blocker` (`python_version=(3, 11)`).
8. `test_api_port_in_use_is_warning` / `test_ports_free_are_ok` (socket connect
   succeeds → warn; raises `OSError` → ok).
9. `test_disk_below_threshold_is_blocker` (fake `disk_usage` free < threshold →
   `INSUFFICIENT_DISK`).
10. `test_capacity_unavailable_is_warning_only` (docker down → capacity unknown,
    but capacity itself never a blocker).
11. `test_shell_path_awf_not_found_is_warning` with PATH hint in `fix`.
12. `test_report_status_rollup` (fail > warn > ok precedence).
13. `test_subprocess_timeout_is_handled` (`TimeoutExpired` → `fail`, bounded, no
    crash, no bare-Exception swallow).
14. `test_checks_do_not_start_core` (no `compose ... up`/bootstrap invocation;
    assert only allowed bounded commands are called via the fake runner).
15. `test_metadata_is_secret_free` (token-shaped env value never appears in
    `report.to_dict()`).

### `tests/unit/service/test_host_setup_rendering.py` (extend)

16. `test_setup_readiness_payload_success` → `status == "success"`, `details`
    has `dry_run`, `providers`, `provider_scope`, `checks`, capacity; next
    command present.
17. `test_setup_readiness_payload_blocked` → `reason_code == SETUP_READINESS_FAILED`,
    `status == "blocked"`, blockers + per-check `docs_link` rendered in pretty,
    `next_steps` include blocker fixes + `awf setup --dry-run`.
18. `test_setup_readiness_payload_warning_succeeds` (warn-only report → exit-0
    style success payload carrying warnings).
19. `test_setup_readiness_payload_redacts_provider_refs` (a provider-ref/token in
    details is redacted by `render_first_run_json`).

### `tests/unit/cli/test_setup_commands.py` (rewrite for real behavior)

CLI tests monkeypatch `awf.cli.setup_commands.run_system_checks` (crafted
report) and config IO (`read_host_setup_config` / `write_host_setup_config`
spies) for determinism.

20. `test_setup_help_describes_real_first_run_surface` (help shows "Prepare this
    machine", `--dry-run`, `--provider`; no "Reserved" language; no `Traceback`).
21. `test_setup_dry_run_ready_success` (healthy report → exit 0, JSON status
    `success`, next command present).
22. `test_setup_dry_run_does_not_write_config_or_secrets` (write spy **not**
    called; if a real tmp config path is used, file absent afterward).
23. `test_setup_dry_run_docker_blocker_readiness_failure` (docker-fail report →
    exit 1, `reason_code == SETUP_READINESS_FAILED`, blockers list docker, next
    actions present, pretty on stderr / stdout empty).
24. `test_setup_provider_selector_forwarded` (`--provider github` → details
    `providers == ["github"]`, `provider_scope == "targeted recheck"`).
25. `test_setup_repeated_provider_selectors_deduped`
    (`--provider github --provider github` → `["github"]`).
26. `test_setup_multiple_providers_forwarded`
    (`--provider github --provider codex` → both, order-stable).
27. `test_setup_unknown_provider_rejected` (`--provider nope` → exit 2,
    `reason_code == SETUP_PROVIDER_UNKNOWN`, `run_system_checks` **not** called).
28. `test_setup_allow_plain_secrets_forwarded_not_default`
    (with flag → details `allow_plain_secrets is True`; without → `False`).
29. `test_setup_source_checkout_valid_dry_run` (`--source-checkout <repo root>` →
    success, details `source_checkout` populated; uses `validate_source_checkout`).
30. `test_setup_source_checkout_invalid` (bad path → exit 1,
    `reason_code == SOURCE_CHECKOUT_INVALID`, `missing_markers` present).
31. `test_setup_non_interactive_provider_requires_input`
    (`--non-interactive --provider github` no `--dry-run` → exit 1,
    `reason_code == INTERACTIVE_INPUT_REQUIRED`).
32. `test_setup_non_dry_run_writes_safe_config` (healthy report, not dry-run →
    write spy called with a `HostSetupConfig` carrying no secret values; consent/
    source recorded).
33. `test_setup_config_corrupt_surfaces_reason` (`read_host_setup_config` raises
    `HostSetupConfigError(HOST_SETUP_CONFIG_CORRUPT)` → failure payload with that
    reason).
34. `test_setup_json_and_pretty_shapes` (JSON keys stable; pretty contains
    `Status:`, blockers, warnings, `Docs:`, `Next:`).

Keep `tests/unit/cli/test_first_run_command_imports.py` green (no import-time
payload construction).

## Implementation Steps (ordered)

1. Add the failing tests above (service, then rendering, then CLI).
2. Implement `host_setup/system_checks.py` (result models + injectable
   `run_system_checks`); make service tests green.
3. Add `first_run_setup_readiness_payload(...)` to `rendering.py`; make rendering
   tests green.
4. Rewrite `cli/setup_commands.py` for real dispatch; update `main.py` help;
   export new symbols from `host_setup/__init__.py`; make CLI tests green.
5. Create `plans/T04_SETUP_DRY_RUN_PLAN.md` (mirror) and, after green,
   `plans/T04_SETUP_DRY_RUN_VALIDATION.md`.
6. Run focused validation (below); iterate on any `Partial`/`Missing` gap.

## Validation Commands And Pass Criteria

Focused, per the AWF agent contract (AWF/GitHub own the broad suite):

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

Manual source-lane spot check (acceptance: works under `uv run`):

```bash
uv run --python 3.12 --extra dev awf setup --dry-run --format json
uv run --python 3.12 --extra dev awf setup --dry-run --source-checkout .
uv run --python 3.12 --extra dev awf setup --provider github --dry-run
```

Pass criteria:

- ruff + mypy clean on touched files.
- All listed test modules pass; catalog-coverage test still passes (no new codes).
- `awf setup --dry-run` performs zero config writes and never starts Core.
- Each T04 acceptance criterion maps to at least one passing test (table below).
- Coverage on new lines kept at/above the repo target (broad coverage gate owned
  by AWF/CI after the agent phase).

## Acceptance-Criteria Traceability

| Acceptance criterion | Covered by |
| --- | --- |
| `--dry-run` never writes secrets / never starts Core | #22, #14, no bootstrap call |
| `--provider github --dry-run` accepts + forwards selector | #24 |
| `--allow-plain-secrets` accepted + forwarded, not default | #28 |
| Unknown provider → reason-coded diagnostic, no all-provider fallback | #27 |
| Missing/stopped Docker → readiness failure with next actions | #23, #2, #3, #17 |
| Source-checkout dry-run works from checkout & `uv run ... --source-checkout .` | #29, manual spot check |
| Pretty output: status, blockers, warnings, docs links, next command | #34, #17 |
| Non-interactive secret-needed → `INTERACTIVE_INPUT_REQUIRED` | #31 |

## Risks, Assumptions, Open Decisions

Assumptions (finalize/adjust in tests during implementation):

- **A1 — Port semantics:** an *occupied* API/Postgres port is a **warning**
  (AWF may already be running), not a hard blocker; a *refused* connection is
  `ok`. Avoids false negatives when Core is up.
- **A2 — `gh` and `shell_path` are warnings**, not blockers: GitHub auth can use
  an env-ref token (T07), and `awf` may legitimately run via `uv run` without a
  global PATH entry.
- **A3 — Provider set** for `--provider` is the credential-bearing providers
  (`github, codex, claude_code, gemini, opencode`); `docker` is excluded (it is a
  system check). Aliases (e.g. `openai`→`codex`) are out of scope unless a test
  demands them.
- **A4 — Interactive guard trigger:** since T04 does not capture credentials,
  `INTERACTIVE_INPUT_REQUIRED` is raised when `--non-interactive` + a `--provider`
  selection + not `--dry-run` would begin provider configuration deferred to
  T06/T07. The backlog phrases this as "if this path is touched", giving latitude.
- **A5 — Safe config write** happens only when not `--dry-run` and not blocked,
  recording non-secret fields (source-checkout metadata, consent flags) merged
  into existing config; never secrets.
- **A6 — Disk threshold** reuses `DEFAULT_MIN_FREE_DISK_BYTES` (10 GiB),
  overridable via the injected parameter for tests.
- **A7 — Capacity** is derived from the single `docker info` JSON used for the
  daemon check (no extra subprocess, no full `Settings`).

Risks / mitigations:

- **Existing placeholder tests change.** `tests/unit/cli/test_setup_commands.py`
  is rewritten (placeholder → real behavior); `AWF_SETUP_PLACEHOLDER` stays in
  the catalog/`rendering` constants (used by tooling) but is no longer emitted by
  the command.
- **Import cycle risk** (`rendering` ↔ `system_checks`). Mitigation: strictly
  one-directional (`rendering` imports `system_checks`); verified by import test.
- **Catalog-coverage guard** could trip on stray reason strings. Mitigation:
  per-check reasons live only in the structured report (no `error_code=`, not in
  `_REASON_TEXT`); top-level payloads use only documented codes.
- **No bare-`except`** per AGENTS.md: subprocess/socket failures catch specific
  exceptions (`FileNotFoundError`, `OSError`, `subprocess.TimeoutExpired`,
  `json.JSONDecodeError`) and map to `fail`/`skipped` with preserved reasons.
- **Determinism / boundedness** (perf review note): all checks are local-first
  with ~5s bounded subprocess and short socket timeouts; tests inject fakes.

## Definition Of Done

- New/edited modules implemented; all listed tests pass; ruff + mypy clean on
  touched files; catalog-coverage and doctor-reason tests still pass.
- `awf setup --dry-run` runs host checks, emits readiness payload in json/pretty,
  writes nothing, starts nothing.
- `plans/T04_SETUP_DRY_RUN_PLAN.md` and
  `plans/T04_SETUP_DRY_RUN_VALIDATION.md` exist with per-requirement status.
- Changes scoped to setup CLI, host setup system checks, rendering integration,
  and tests; no work belonging to T05–T08 is implemented.
