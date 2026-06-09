# AWF MVP — Parallel-Agent Execution Substrate for Aira

> Historical design note: this plan records the original Aira-oriented MVP
> framing that informed the public Agent Workspace Fabric (AWF) alpha. It is
> preserved as implementation history, not current release guidance.

## Context

Dmitri's primary constraint on shipping aira features is **parallel agent throughput**, not feature scope or agent capability. Today, each PR takes ~8 hours end-to-end (largely review + conflict resolution), and the workflow is serial — he babysits one PR at a time. Concurrent agent demand is 8+ but suppressed by the lack of a substrate that can isolate agents safely.

**AWF (Agent Workspace Fabric)** is a standalone execution service that any orchestrator (OpenClaw agents via skill, aira-agent's own supervisor, a human-triggered CLI, etc.) can call to run one coding task end-to-end in an isolated Docker workspace: check out source, launch a **coding CLI** (Codex / Claude Code / Gemini) inside the container with the repo mounted, run tests (with sidecar services like Postgres + Alembic migrations as the repo profile requires), and submit a PR against the development branch. The distinction matters: **AWF is called by orchestrators; the actual code-writing is done by the coding CLI inside the container.**

This MVP intentionally ships the "developer-in-a-box" primitive only. Owned paths are stored as coordination hints and stale-detection inputs; overlapping owned paths are admitted and surfaced as overlap-risk warnings. The stale-detection / auto-rebase / merge-queue / explicit exclusive-lock machinery from the full AWF v2.2 PRD is explicitly deferred to Phase 1.5, pending evidence from real parallel runs about which conflicts actually happen in practice.

### Design decisions (confirmed with Dmitri 2026-04-21)

- **Stateful API** — full control + observability from day 1 (workspaces table, async operation IDs, polling, retries, debug pins).
- **Dual surface** — REST (for OpenClaw via skill, shell-style invocation) + MCP server (for Codex / Claude Code to invoke AWF as a typed tool) on the same underlying service.
- **Python 3.11 + FastAPI + mcp SDK** for the MVP. Go reserved as a later optimization for the node-worker if/when Python is the measured bottleneck.
- **Agent-agnostic** via a pluggable adapter layer. The three adapters are the three real coding CLIs we launch inside the workspace container: **Codex** (`codex exec`), **Claude Code** (`claude` headless), and **Gemini CLI** (`gemini --yolo`). OpenClaw is intentionally *not* an adapter — it's a model-routing gateway, not a coding CLI.

### Non-goals for MVP (deferred to Phase 1.5+)

- Stale detection on target-branch advance
- Auto-rebase / rebase orchestration
- Merge queue / canonical-attempt governance
- Task-class exclusive resource lock matrix (docs/test/refactor/migration/dependency/build_config)
- Merge-time overlap resolution beyond advisory owned-path risk events
- Three-tier validation (MVP has one tier: task-local validation inside the workspace)
- Post-merge confidence validation
- Multi-node execution / node-agent federation (MVP is single-host Docker)
- GCP / GKE backend
- Per-repository policy configuration (MVP hardcodes Aira dogfooding profile)
- Sophisticated failure taxonomy (MVP uses a minimal enum: `agent_failure`, `validation_failure`, `infrastructure_failure`)

## Approach

### Architecture overview

```
                       ┌──────────────────────────────────────────┐
                       │ AWF Control Plane (single FastAPI process)│
                       │                                          │
  REST (OpenClaw) ───▶ │  /v1 REST API         MCP server         │ ◀─── MCP (Codex / Claude Code)
                       │         │                 │              │
                       │         └────────┬────────┘              │
                       │                  ▼                       │
                       │            workspaces / operations /     │
                       │            events state (Postgres)       │
                       └────────────────┬─────────────────────────┘
                                        │
                                        ▼ (async worker — SELECT FOR UPDATE SKIP LOCKED)
                       ┌──────────────────────────────────────────┐
                       │ Node Worker (same process for MVP)       │
                       │                                          │
                       │  git worktree mgr → compose provisioner  │
                       │  → agent adapter (Codex|ClaudeCode|Gemini)│
                       │  → validation runner → PR creator        │
                       │  → cleanup                               │
                       └──────────────────────────────────────────┘
                                        │
                                        ▼ (local Docker daemon)
                       ┌──────────────────────────────────────────┐
                       │ Per-workspace Compose project            │
                       │   agent-runtime  +  Postgres sidecar     │
                       │   (optional: Redis, app-under-test)      │
                       └──────────────────────────────────────────┘
```

### Repo structure

```
agent-workspace-fabric/
├── README.md
├── pyproject.toml
├── uv.lock
├── .env.example
├── docker/
│   ├── control-plane.Dockerfile
│   ├── agent-runtime.Dockerfile           # baseline image for coding agents
│   └── compose/
│       ├── workspace.base.yml.j2          # Jinja2 template, agent + Postgres
│       └── repo-profiles/
│           └── aira.yml                   # aira-agent repo profile (deps, migrations, tests)
├── migrations/                            # Alembic for control-plane DB
├── src/
│   └── awf/
│       ├── api/                           # FastAPI routes, Pydantic schemas
│       ├── mcp/                           # MCP server, tool registration
│       ├── control/                       # async worker, operation queue, state machine
│       ├── db/                            # SQLAlchemy models, repositories
│       ├── node/                          # git worktree mgr, compose provisioner, cleanup
│       ├── adapters/                      # codex.py, claude_code.py, gemini.py (one interface)
│       ├── runtime/                       # validation runner, artifact + log capture
│       ├── cli/                           # Typer CLI
│       └── common/                        # config, events, structured logging
├── tests/
│   ├── unit/
│   ├── integration/                       # uses testcontainers for real Postgres
│   └── e2e/                               # full flow against a sandbox repo
└── docs/
    ├── PLAN_MVP.md                        # this file
    ├── API.md                             # generated from OpenAPI spec
    └── ADR/                               # architecture decisions
```

### API surface (MVP)

All endpoints under `/v1`, all mutating endpoints accept `Idempotency-Key`.

| Endpoint | Method | Description |
|---|---|---|
| `/v1/workspaces` | `POST` | Create workspace → returns `202 Accepted` + `workspace_id` + `status_url`. Body fields: `repo` (url + default_branch + base_branch), `task` (task_id optional, title, prompt, agent, env_profile), `validation` (test_commands, requires_database), `resources` (optional overrides), `wait_timeout_seconds` (sync-mode try-timeout). |
| `/v1/workspaces/{id}` | `GET` | Current state + version + linkages |
| `/v1/workspaces` | `GET` | List with cursor pagination + filters (status, agent, repo) |
| `/v1/workspaces/{id}/cancel` | `POST` | Request cancellation → returns operation record |
| `/v1/workspaces/{id}` | `DELETE` | Destroy (cleanup) → returns operation record |
| `/v1/workspaces/{id}/logs` | `GET` | Log metadata + retrieval URLs |
| `/v1/workspaces/{id}/artifacts` | `GET` | Artifact metadata + retrieval URLs |
| `/v1/workspaces/{id}/artifacts/download?path={relative_path}` | `GET` | Token-protected artifact bytes from the workspace artifact root only |
| `/v1/operations/{id}` | `GET` | Async operation status |
| `/v1/events` | `GET` | Event stream with cursor + filters |

### MCP surface (MVP)

Same underlying service, exposed as MCP tools so Codex / Claude Code can invoke AWF without shelling out to curl:

| Tool | Description |
|---|---|
| `awf_create_workspace` | Rich profile-driven body matching `POST /v1/workspaces`. Returns `workspace_id`. |
| `awf_get_workspace` | Fetch current state by `workspace_id`. |
| `awf_wait_for_workspace` | Blocking helper: polls until terminal state or `timeout_seconds`. Useful for agents that want synchronous behavior. |
| `awf_list_workspaces` | List with filters. |
| `awf_list_workspace_events` | List one workspace's immutable events newest-first. |
| `awf_list_workspace_logs` | List indexed durable log streams for one workspace. |
| `awf_read_workspace_log` | Read a bounded log chunk by stream id and byte offset. |

MCP stays read-only beyond create in the always-on service. Destructive controls
remain on the authenticated REST/operator surface.

### Workspace lifecycle (MVP)

Simplified from the full PRD — no `stale`, no `rebasing`, no `validating_tier2` / `tier3`:

```
requested → provisioning → ready → running → validating → pushing → completed
                     │        │       │           │           │          │
                     ▼        ▼       ▼           ▼           ▼          ▼
                       failed / cancelled ────────────────────┴─▶ destroying → destroyed
```

### Agent adapter interface

Abstract interface (`awf/adapters/base.py`):

```python
class AgentAdapter(Protocol):
    name: str  # "codex" | "claude_code" | "gemini"

    async def prepare_container(self, workspace: Workspace) -> None:
        """Install/verify the agent CLI inside the workspace container."""

    async def run(self, workspace: Workspace, prompt: str) -> AgentRunResult:
        """Execute the coding task. Returns structured result (commits made, files changed, errors)."""

    async def health_check(self) -> bool: ...
```

Implementations: `codex.py`, `claude_code.py`, `gemini.py`. All three are structurally similar (async subprocess with CLI-specific flags + prompt injection + output parsing); wire Codex first because it's the most mature headless CLI, then Claude Code, then Gemini.

### Compose template (MVP)

One Jinja2 template (`docker/compose/workspace.base.yml.j2`) rendered per workspace, with variables for `workspace_id`, `agent_runtime_image`, `branch`, `env_profile`, and per-profile additions. Baseline services:

- `agent`: the coding-agent runtime container, mounts the worktree, holds Postgres connection env
- `postgres`: persistent per-workspace Postgres volume, only the agent container can reach it (network scope)
- Profile-driven optional: `redis`, `app-under-test` (started on demand during validation)

Resource defaults per AWF PRD Section 7.3: steady-state ~3 CPU / 10 GB; peak ~6 CPU / 16 GB for the burst.

### Concurrency model

- API server: FastAPI with uvicorn, async handlers.
- Background worker: single process in the same container for MVP, picks up operations from a Postgres-backed queue via `SELECT ... FOR UPDATE SKIP LOCKED`. No Redis / Celery dependency.
- Single-host Docker: worker shells out to `docker compose` via typed Python wrappers (`src/awf/node/compose_manager.py`). Multi-node deferred.

### Critical files to create (MVP order)

| Path | Purpose |
|---|---|
| `pyproject.toml` | Deps: fastapi, uvicorn, sqlalchemy, alembic, psycopg, pydantic-settings, mcp, typer, jinja2, python-docker, httpx, pytest, testcontainers |
| `src/awf/api/app.py` | FastAPI app factory, route registration, CORS |
| `src/awf/api/routes/workspaces.py` | REST endpoints for workspace lifecycle |
| `src/awf/api/routes/operations.py` | Operation status endpoint |
| `src/awf/api/schemas.py` | Pydantic request/response models (shared with MCP) |
| `src/awf/mcp/server.py` | MCP server setup, tool registration, delegation to REST handlers |
| `src/awf/control/worker.py` | Async operation worker with FOR-UPDATE-SKIP-LOCKED |
| `src/awf/control/state_machine.py` | Enforces workspace state transitions |
| `src/awf/db/models.py` | SQLAlchemy models: Workspace, Operation, Event |
| `src/awf/db/repositories.py` | Data access with optimistic concurrency |
| `src/awf/node/git_manager.py` | Bare mirror + worktree create/refresh/cleanup |
| `src/awf/node/compose_manager.py` | Jinja2 template rendering + `docker compose` subprocess |
| `src/awf/node/provisioner.py` | Orchestrates git + compose for one workspace |
| `src/awf/node/cleanup.py` | Compose down + volume removal + event emit |
| `src/awf/adapters/base.py` | AgentAdapter Protocol + registry |
| `src/awf/adapters/codex.py` | First adapter (Codex — most mature headless coding CLI) |
| `src/awf/runtime/validation.py` | Runs `test_commands` inside container, captures artifacts/logs |
| `src/awf/runtime/pr_creator.py` | `git push` + `gh pr create --base development` |
| `src/awf/cli/main.py` | Typer CLI mirroring REST API |
| `migrations/versions/0001_initial.py` | Schema: workspaces, operations, events |
| `docker/control-plane.Dockerfile` | Control-plane service image |
| `docker/agent-runtime.Dockerfile` | Baseline multi-arch (x86_64 + arm64) agent container |
| `docker/compose/workspace.base.yml.j2` | Workspace compose template |
| `docker/compose/repo-profiles/aira.yml` | Aira-specific: PY 3.12, Alembic, pytest, Postgres version |

### Reused from existing aira-agent context (do not rewrite)

- `clawdbot/pr-manager.sh`, `clawdbot/skills/pr-review-hygiene/SKILL.md`, `clawdbot/skills/swarm/SKILL.md` — AWF's post-PR workflow can hand off to these later. For MVP, PR lifecycle after submission is out of scope; aira-backend continues to own it.
- OpenClaw gateway patterns from `aira-agent/docker/agent/entrypoint.sh` — inform the agent-runtime Dockerfile.

## Task breakdown (~6-8 tasks, each 1-2 engineer-days)

| # | Task | Exit criterion | Files |
|---|---|---|---|
| 1 | **Scaffold repo + API skeleton** | `pyproject.toml`, FastAPI app, Pydantic schemas, Alembic init, Postgres schema for workspaces/operations/events. `curl POST /v1/workspaces` returns a workspace_id in `requested` state. | `pyproject.toml`, `src/awf/api/**`, `src/awf/db/**`, `migrations/versions/0001_initial.py` |
| 2 | **Git worktree manager** | Bare mirror creation + worktree creation from base SHA. Workspace advances `requested → provisioning → ready` with a real checkout on disk. | `src/awf/node/git_manager.py`, `src/awf/control/worker.py` |
| 3 | **Compose provisioner + Postgres sidecar** | Jinja2 template renders, `docker compose up` launches agent + Postgres, health-check waits for Postgres readiness. Workspace `ready` event emitted only once DB is reachable. | `docker/compose/workspace.base.yml.j2`, `src/awf/node/compose_manager.py`, `src/awf/node/provisioner.py` |
| 4 | **Codex adapter (first)** | Subprocess invocation with prompt injection + result parsing. End-to-end `POST /v1/workspaces` with a trivial task (e.g., "add a docstring to file X") produces a commit on the feature branch inside the workspace. | `src/awf/adapters/base.py`, `src/awf/adapters/codex.py` |
| 5 | **Validation runner** | Executes `test_commands` inside container against workspace-local Postgres (Alembic migrate + pytest). Captures logs + artifacts. Workspace transitions `running → validating → pushing` on pass, `failed` on fail. | `src/awf/runtime/validation.py`, `src/awf/runtime/artifacts.py` |
| 6 | **PR creator + cleanup** | `git push` + `gh pr create --base development`. `DELETE /v1/workspaces/{id}` runs compose down + volume removal. Full lifecycle returns a real PR URL; no orphaned containers after destroy. | `src/awf/runtime/pr_creator.py`, `src/awf/node/cleanup.py` |
| 7 | **MCP server surface** | Expose `awf_create_workspace`, `awf_get_workspace`, `awf_wait_for_workspace`, `awf_cancel_workspace`, `awf_list_workspaces` as MCP tools. A Claude Code session can invoke `awf_create_workspace` and get a PR back. | `src/awf/mcp/server.py` |
| 8 | **CLI + minimal dashboard** | `awf workspace create/list/show/cancel/destroy/logs` via Typer. Optional: HTMX/Tailwind dashboard at `:8080` showing workspace list + timeline + logs. | `src/awf/cli/main.py`, (optional) `src/awf/dashboard/**` |

**Estimated total: 8-12 engineer-days** (vs. full PRD Phase 1 at 31-44). Task 8's dashboard can be deferred if pressed for time — CLI alone is enough for MVP.

## Verification

**Unit + integration:**
```bash
pytest tests/unit/ -v
pytest tests/integration/ -v                # spins real Postgres via testcontainers
```

**End-to-end (local):**
1. `cd ~/Projects/agent-workspace-fabric && docker compose up control-plane postgres -d`
2. `awf workspace create --repo git@github.com:dimileeh/aira-agent.git --base development --agent codex --title "trivial docstring" --prompt "Add a one-line docstring to src/aira_agent/api/main.py explaining the module." --test 'ruff check .' --test 'pytest tests/unit/ -q'`
3. Poll `awf workspace show <id>` — expect `provisioning → ready → running → validating → pushing → completed`
4. Verify PR URL returned, PR exists on GitHub targeting `development`
5. `awf workspace destroy <id>` — verify `docker ps -a | grep awf` is empty after destroy

**Parallelism smoke test:**
1. Submit 3 workspaces simultaneously (same repo, different tasks, different file scopes)
2. Verify all 3 reach `ready` within 30s; overlapping owned paths may produce advisory risk warnings but must not block admission
3. Verify all 3 produce separate PRs
4. Verify cleanup of all 3 leaves no orphaned containers or volumes

**MCP verification:**
1. Start the AWF server
2. Register it as an MCP server in a Claude Code session
3. Invoke `awf_create_workspace` from chat with the same parameters as step 2 above
4. Verify workspace progresses end-to-end and PR URL is returned to the chat

## Phase 1.5 (next, after MVP ships and is dogfooded for ~2 weeks)

Once the MVP is in daily use for Aira dev, collect data on which conflicts actually happen:
- Stale branches when target advances during long validation
- Overlapping edits across parallel workspaces
- Alembic migration collisions
- Merge conflicts at `gh pr create` time

Build the Phase 1.5 pieces *targeted at observed incidents*, not at the PRD's full list. Likely subset:
- Stale detection + auto-rebase on target advance (if stale branches show up often)
- Lightweight merge queue for the `development` branch (if overlapping merges become the babysitting pain)
- Explicit exclusive resource locks (only for classes/resources that actually need serialization; do not promote ordinary owned-path overlap into blocking by default)

Do NOT build:
- Three-tier validation unless Tier 1 alone proves insufficient
- Canonical-attempt / task-attempt lineage unless retries become frequent
- Multi-node execution until a single DGX is saturated
