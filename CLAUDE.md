# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AWF (Agent Workspace Fabric) is the **control plane** for running AI coding agents (Codex,
Claude Code, Cursor, Gemini, OpenCode, Grok) as disciplined contributors. Each coding task gets an isolated
git worktree, a per-workspace Docker Compose stack, profile-driven validation, a created PR,
and a PR-monitor loop that handles review comments, CI fixes, base sync, and auto-merge gates
before cleanup. AWF owns lifecycle/policy; the agent owns code changes; profiles own
project-specific setup; GitHub stays the source of truth for PRs/checks.

This repo is the **alpha local Core** (Python 3.12, FastAPI, SQLAlchemy 2.0 async, Pydantic v2,
Alembic, Docker SDK, structlog). Hosted/GKE/multi-tenant layers are future work.

Read `AGENTS.md` first — it carries the binding engineering rules summarized below.
`docs/CONCEPTS.md` is the authoritative architecture + glossary; `docs/awf_prd_v2.2.md` is the
product contract when behavior is ambiguous.

## Essential commands

All Python commands run through `uv` against Python 3.12 with the `dev` extra.

```bash
# Setup (contributor checkout)
uv sync --extra dev
npm --prefix apps/console ci          # only if touching the console

# The full local-validation gate (matches CI; run before pushing)
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev ruff format --check .
uv run --python 3.12 --extra dev mypy              # no path args — pyproject pins files = ["src/"]
uv run --python 3.12 --extra dev pytest -q

# Narrow first, then widen. Single dir / file / test:
uv run --python 3.12 --extra dev pytest tests/unit/api -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_parts -q -k <name>

# Coverage (hard 99% gate — see below)
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing

# OpenAPI drift gate (fails if openapi.json diverges from the FastAPI app)
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check

# Console (Next.js)
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
npm --prefix apps/console run test:browser     # Playwright browser smoke
```

Pre-commit hooks (`awf-ruff-check`, `awf-ruff-format-check`, `awf-mypy`) run these **without
auto-fixing** — fix manually, then re-run or `pre-commit run --all-files`.

**Console design system:** read `apps/console/DESIGN.md` before any console UI work. It is the
source of truth for typography (IBM Plex), the semantic color tokens, status glyphs, density, and
the Status/Diagnosis/Action layout. Do not hardcode palette values or convey status by color alone.

**Running the control plane locally** (Postgres + one-shot Alembic `migrate` + API + worker, all
in Docker Compose):

```bash
cp .env.example docker/compose/.env        # set AWF_API_TOKEN here
uv run --python 3.12 --extra dev awf service bootstrap        # idempotent; safe to re-run
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf service logs --follow --service worker
```

`awf serve --reload` runs the API alone for throwaway dev. `awf service readiness --format json`
(alias `awf service release-readiness`) is the local Core release scorecard. `awf init <path>`
onboards a target project; `awf profile preview . --profile auto` shows the resolved profile.
The control-plane image is **not** bind-mounted — rebuild (`awf service bootstrap`) to pick up
code changes.

### CI gate

CI requires the `ci-required` rollup job, which depends on `lint-and-type`,
`python-full-coverage` (aggregate 99% coverage gate), `console`, and `release-artifacts`.
Coverage execution runs in parallel through `python-coverage-shards`, then
`python-full-coverage` combines the shard artifacts and runs
`scripts/ci/check_coverage_threshold.py`.

## Non-negotiable engineering rules (from AGENTS.md)

These override default behavior. They are project-specific, not generic boilerplate.

- **Strict TDD for behavior changes.** Write/adjust the failing test first, then implement the
  smallest green change. Every bug fix needs a regression test or it is considered unfinished.
- **99% coverage is enforced**, not aspirational (`pyproject.toml fail_under = 99`, CI, and
  `.awf/workspace.yml`). If you can't hit it, leave coverage measurably closer and explain.
- **Never hide failures behind retries.** Retries must preserve reason codes, logs, and events.
  Catch specific exceptions, not bare `Exception`.
- **Keep AWF core generic.** Project-specific runtime/services/secrets belong in profiles
  (e.g. the `aira` profile), never hard-coded into `src/awf` core.
- **Keep changes scoped**; prefer existing patterns in `src/awf` over new abstractions.
- **Plan-and-Validate for non-trivial work** (`plans/PLAN_EXECUTION_PROTOCOL.md`): save the plan
  to `plans/<TOPIC>_PLAN.md` before coding, validate against it in `plans/<TOPIC>_VALIDATION.md`.
- **Never log secrets**; no destructive git ops against user work; preserve unrelated changes.

## Architecture

### Layering and dependency direction

```text
  client surfaces            business logic         orchestration            execution substrate
  ───────────────            ──────────────         ─────────────            ───────────────────
  api/   (FastAPI /v1)  ┐                       ┌─ control/worker  (poll+claim+dispatch loop)
  cli/   (httpx→REST)   ├──▶  service/   ──────▶│  control/executor (one workspace: agent→
  mcp/   (in-process)   ┘     (WorkspaceService, │                    validate→push→monitor)
                              controls, scheduler,│
                              capacity, gc,       ├─ node/     (git worktrees + Compose stacks)
                              merge_queue, ...)   ├─ runtime/  (PR-monitor loop, validation,
                                                  │             Plan→Execute→Compare planning)
                                                  └─ adapters/ (agent CLI wrappers)

  cross-cutting:  db/ (SQLAlchemy async repos + models)   profiles/ (workspace.yml schema)
                  common/ (github_client, redaction, audit, logging, ids, reason codes)
```

- **`api/`** — thin FastAPI routes under a single `/v1` namespace. Routes contain no business
  logic; they translate to `service/` calls and map domain exceptions to `HTTPException` with an
  `error_code`. App is built by `create_app()` in `api/app.py`. Request/response schemas live in
  `api/schemas.py` and are **reused by the MCP server** — keep them in lockstep, and regenerate
  `openapi.json` when they change (drift gate).
- **`cli/`** (Typer, entry `awf`) is a **thin httpx client over REST** — it needs a running API
  (`awf serve`). **`mcp/`** (FastMCP) is **in-process**, closing over `WorkspaceService` directly.
  Both mirror the REST contract 1:1 (command groups: `workspace`, `profile`, `service`, `locks`,
  `operations`, `mcp`, `smoke`).
- **`service/`** is the largest layer: stateless business operations over the DB. Key seams:
  `controls.py` (cancel/stop/destroy/remonitor/validate/rebase, idempotency, state-machine guards),
  `workspaces.py` (`WorkspaceService` facade used by both REST and MCP), `scheduler.py`
  (deterministic scoring), `resource_capacity.py`/`local_capacity.py` (admission), `gc*.py`
  (retention cleanup), `merge_queue.py`, `failure_causality.py`.
- **`control/`** is the orchestration core. `worker/manager.py` (`ControlWorker`) polls the DB
  each cycle, claims pending workspaces via DB-backed leases, and dispatches three paths:
  `requested→provisioning`, `ready→execution`, `monitoring_pr→monitor-resume`.
  `executor/execution_flow.py` drives a single workspace through the agent run, post-agent
  commit/repair, the validation fix-cycle, push, PR creation, and PR-monitor handoff.
- **`runtime/`** owns the PR monitor and validation. `validation_runner.py` runs profile phases.
  `planning.py` is the provider-neutral Plan→Execute→Compare lifecycle.
- **`node/`** is the provisioner: `git_manager.py` maintains per-repo bare mirrors and linked
  worktrees; `compose_manager.py` renders the Jinja2 template into a per-workspace Compose stack.
- **`adapters/`** wrap each agent CLI behind one `AgentAdapter.run()` contract; `defaults.py` is
  the single source for default agent model + effort (overridable per-workspace via `task_policy`).
- **`profiles/`** define and resolve `workspace.yml` (runtime, services, phases, validation,
  security, secrets). **`db/`** + **`common/`** are the shared foundation.

### Workspace task lifecycle

State is guarded by `control/state_machine.py` (see invariant below):

```text
requested → provisioning → ready → running → validating → pushing → monitoring_pr → completed
        (any) → failed (preserved for inspection) | cancelled | destroying → destroyed
```

`monitoring_pr` is where AWF acts as a PR owner. The pure decision core
`runtime/pr_monitor.decide()` (no I/O, unit-testable) returns exactly one action —
`AddressComments`, `ReportCiFailure`, `SyncBase`, `WaitForCI`, `Merge`, `NotifyHuman`,
`ShortCircuitCompleted`, `Abort` — and `pr_monitor_runner/runner.py` performs the I/O and
persists `MonitorState`. Feature vs release monitors differ **only** by `auto_merge` (release PRs
return `NotifyHuman` on green instead of merging). Merge gates are conservative: comments
addressed, checks green, GitHub-mergeable, branch not behind, plus a final pre-merge settle.

### Domain model (`db/models.py`)

`Workspace` (the operator-facing unit; versioned, append-only `WorkspaceEvent` audit log,
DB-backed claim leases) · `Task` + `TaskAttempt` (logical work + retry lineage; one attempt is
`is_canonical_for_merge`) · `ValidationRun` (test/coverage provenance) · `Operation` (async
operation audit with idempotency keys) · `MergeCandidate`/`QueueDecision` (PR + scheduler state)
· `ResourceReservation`, `StaleReason`, `PolicyFinding`, `WorkspaceSecretLease`,
`CallbackSubscription`/`Delivery`, `ProviderModelCircuitBreaker`, `EgressAuditRecord`.

### Cross-cutting invariants (these bite if ignored)

- **State transitions** must route through `WorkspaceStateMachine.assert_transition(from, to)`.
  Direct `workspace.status = ...` is a bug. The guard is an explicit call, not a decorator — the
  linter won't catch a forgotten one.
- **Repositories flush but never commit.** Commit happens at the boundary: the FastAPI
  `get_db_session` dependency (auto-commit/rollback) or `session_scope()` in workers. Sessions
  are async and not thread-safe — one per unit of work.
- **Status is stored as strings, not SQL enums** (to avoid migrations on new states). The
  vocabulary lives in `db/enums.py` as dependency-free literals shared by schemas, the state
  machine, and tests.
- **Reason codes flow end-to-end**: exception `reason_code` → structured log field → DB
  `WorkspaceEvent` → `FailureReason` → policy action. Don't swallow them. The catalog is generated
  (`scripts/generate_reason_catalog.py`, `docs/REASON_CATALOG.md`).
- **Idempotency is pervasive**: `idempotency_key` unique constraints, `exact_replay` vs
  `active_coalesce` semantics, plus an in-memory replay cache backed by a DB advisory lock.
- **Redaction is mandatory** for secrets: `common/redaction.redact_secrets()` for live logs,
  `common/audit.redact_audit_value()` for persisted payloads. Logging is structlog JSON only
  (`common/logging.get_logger()`).
- **Profiles resolve once at provision time** (inline → repo `.awf/workspace.yml` → registry
  `profile_ref` → auto-detect → `generic`) and are persisted in `workspace.resolved_profile`;
  later stages reconstruct from that JSON, not by re-resolving.
- **Plan artifacts** under `docs/awf-plans/*` are excluded from the "did anything change?" check.
  An agent that writes only plan files fails with `PLAN_ONLY_OUTPUT` — user-visible work must exist.

### Why files are split so finely

Both `ControlWorker` and `WorkspaceExecutor` are thin shells that wire ~40–80 methods from
focused sibling modules via `*DelegatesMixin` (e.g. `executor/git_methods.py`,
`worker/recovery_stale.py`). When changing behavior, edit the focused module; the base class only
wires it. This is why `control/` spans dozens of small files rather than a few large ones.

## Operational notes

- The control-plane containers run as **root**; the agent runtime runs as unprivileged `agent`
  (uid/gid 1000), and worktrees are chowned to 1000. **Do not `rm` workspace state from the host
  shell** — use `awf service gc` so the in-container worker cleans up with correct ownership.
- The control-plane DB is **PostgreSQL** (asyncpg; `db/session.make_engine` enforces it). The
  AWF Postgres holds only control-plane data — project/workspace databases stay profile-isolated
  in the per-workspace Compose stack. `db/repositories/base.py` carries dialect-aware SQL
  (Postgres JSONB and SQLite `json_extract`) used for tests/introspection.
- For substantial AWF work, prefer launching an **AWF workspace** (dogfood) over doing everything
  by hand, and do not manually resolve comments or merge PRs that AWF's monitor owns unless asked.
- Test layout mirrors `src/` under `tests/unit/`, with large suites split into `test_*_parts/`
  directories. Markers: `unit`, `integration`, `e2e`, `docker`, `slow` (asyncio auto-mode, 30s
  default timeout). See `docs/test-quality-guardrails.md`.
