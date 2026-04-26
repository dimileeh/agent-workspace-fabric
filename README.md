# Aira Agent Workspace Fabric (AWF)

**AWF is an industrial workspace fabric for AI coding agents.**

It gives Codex, Claude Code, Gemini, and future coding agents a repeatable way
to work like disciplined software contributors: each task gets an isolated
workspace, a clean checkout, declared services, validation, PR creation, PR
review monitoring, comment-fix loops, merge gates, artifacts, events, and
cleanup.

AWF is not a chatbot and not a planner. It is the execution substrate beneath a
planner such as Aira, a human operator, or an MCP client.

## The Problem

AI coding agents can write code, but raw agent execution does not scale to a
real engineering workflow.

Without a workspace fabric, parallel agent development quickly runs into the
same operational failures:

- Agents share local state, credentials, databases, Docker networks, or
  dependency caches.
- A task passes tests against an old base branch and becomes stale before merge.
- Review comments arrive after a PR initially looks green.
- CI failures and reviewer feedback require manual babysitting.
- Agents push branches but leave humans to handle comments, conflicts, and
  merge readiness.
- Project-specific setup leaks into the orchestration code.
- Failed workspaces are hard to inspect because logs, events, and reason codes
  are scattered.
- The same runner is hard-coded for one project and cannot be reused for a
  Python, Node, Next.js, Docker Compose, Go, Java, C++, or Rust repository.

The real bottleneck is not whether an agent can edit files. The bottleneck is
whether many agents can safely work on real repositories without requiring a
human to supervise every PR by hand.

## The AWF Solution

AWF turns one coding task into a durable, observable lifecycle:

1. Create a workspace row in the control-plane database.
2. Create an isolated git worktree from the requested base branch.
3. Resolve a workspace profile that describes the project runtime.
4. Render and launch a per-workspace Docker Compose stack.
5. Run profile setup phases.
6. Run the selected coding agent inside the workspace container.
7. Run profile validation phases and explicit request validation commands.
8. Commit, push, and open a pull request.
9. Monitor the PR until it is merged, closed, or failed.
10. Address meaningful review comments by invoking the same agent again.
11. Fix CI failures when logs are available.
12. Sync the base branch into the PR branch when needed.
13. Respect reviewer timing through an initial review grace window.
14. Auto-merge only after all gates pass.
15. Tear down successful workspaces and preserve failed ones for inspection.

Project-specific knowledge belongs in workspace profiles. The AWF control plane
owns generic lifecycle concerns: git isolation, agent execution, service
orchestration, validation, artifacts, PR creation, monitoring, merge safety, and
cleanup.

## Current Status

This repository is an active MVP moving toward the full AWF v2.2 product
contract.

Implemented now:

- FastAPI REST API.
- Typer CLI.
- MCP server primitives.
- SQLAlchemy control-plane models for workspaces, operations, and events.
- Profile-driven workspace resolution.
- Per-workspace Docker Compose stack generation.
- Codex, Claude Code, and Gemini adapters.
- Central default model/effort map for agent adapters.
- Generic phase-based validation.
- Git worktree provisioning.
- PR creation.
- Feature PR monitor with automated comment handling and auto-merge.
- Release/sync PR monitor variants that keep workspaces alive until human merge.
- Initial PR review grace period before auto-merge.
- Non-actionable bot status comment filtering.
- `/v1/events` for workspace timelines.
- Filterable `/v1/workspaces` list endpoint for future dashboard work.
- Stranded feature-PR watchdog (`awf-watchdog`) for reattaching dead monitors.

Still not complete:

- Full merge queue across multiple task PRs.
- Full task-class lock matrix.
- Full stale/canonical attempt model.
- Multi-node scheduling.
- Cloud backend.
- Full web dashboard.
- Full secret lease broker.

See:

- [docs/awf_prd_v2.2.md](docs/awf_prd_v2.2.md) for the end-state PRD.
- [docs/PLAN_MVP.md](docs/PLAN_MVP.md) for the MVP plan.
- [docs/PLAN_PR_MONITOR.md](docs/PLAN_PR_MONITOR.md) for PR monitor design.
- [docs/PLAN_RELEASE_PR_SYNC.md](docs/PLAN_RELEASE_PR_SYNC.md) for release PR sync.

## Architecture

```text
        Human / Aira / MCP client / local script
                         |
                         v
        +------------------------------------+
        | AWF Control Plane                  |
        |                                    |
        | FastAPI REST API                   |
        | MCP tools                          |
        | Typer CLI                          |
        | Workspaces / operations / events   |
        +-----------------+------------------+
                          |
                          v
        +------------------------------------+
        | Workspace Orchestration            |
        |                                    |
        | git mirror + worktree              |
        | profile resolver                   |
        | compose renderer                   |
        | validation runner                  |
        | PR creator                         |
        | PR monitor                         |
        +-----------------+------------------+
                          |
                          v
        +------------------------------------+
        | Per-Workspace Docker Compose Stack |
        |                                    |
        | agent container                    |
        | optional profile services          |
        | optional per-workspace DinD        |
        | mounted repo at /workspace         |
        +-----------------+------------------+
                          |
                          v
        +------------------------------------+
        | Coding Agent CLI                   |
        |                                    |
        | codex / claude / gemini            |
        | edits files                        |
        | commits changes                    |
        | fixes review comments              |
        | fixes CI failures when possible    |
        +------------------------------------+
```

The important design boundary:

- AWF owns lifecycle and policy.
- The coding agent owns code changes inside the workspace.
- Workspace profiles own project-specific setup.
- GitHub remains the PR/review/check source of truth.

## Workspace Profiles

Profiles make AWF universal rather than Aira-specific.

A `WorkspaceProfile` can describe:

- `runtime`: agent image, toolchain image, environment variables.
- `docker`: no Docker or per-workspace DinD.
- `services`: profile-declared sidecars.
- `phases`: setup, pre-agent, post-agent, validate, cleanup commands.
- `validation`: health checks, artifact paths, timeout and tier hints.
- `monitor`: PR-monitor policy such as initial review grace.
- `secrets`: named mounts or env leases.
- `ports`: endpoint names exposed to agents or tests.

Resolution order:

1. Inline profile in the v2 request.
2. Repo-local `.awf/workspace.yml`.
3. Central built-in profile registry by `profile_ref`.
4. Auto-detection.
5. Low-confidence `generic` fallback.

Built-in profile directions include:

| Profile | Purpose |
| --- | --- |
| `generic` | Agent-only workspace. Caller or repo profile supplies validation. |
| `python` | Detects `pyproject.toml` or `requirements.txt`; installs dev deps and runs `pytest -q`. |
| `node` | Detects `package.json`; chooses npm, pnpm, yarn, or bun from lockfiles. |
| `nextjs` | Node profile plus Next.js lint/test defaults when `next` is present. |
| `docker-compose` | Enables per-workspace DinD and runs project Compose inside it. |
| `aira` | Compatibility profile for Aira's Postgres/pgvector/Alembic expectations. |

For AWF itself, the repo-local profile is:

```yaml
awf:
  name: awf-self
  version: 1
  description: AWF self-dogfood profile for Python CLI/control-plane work.
  docker:
    mode: none
  runtime:
    environment:
      PYTHONUNBUFFERED: "1"
  phases:
    setup:
      - command: uv sync --extra dev
        timeout_seconds: 900
    validate:
      - command: uv run ruff check src/awf/cli tests/unit/cli
        timeout_seconds: 300
      - command: uv run mypy src/awf/cli
        timeout_seconds: 300
      - command: uv run pytest tests/unit/cli -q
        timeout_seconds: 300
```

Omitted fields use model defaults. For example, the PR monitor grace window is
900 seconds even though this self-profile does not spell it out.

Preview the profile AWF would resolve for a checkout:

```bash
uv run --python 3.12 --extra dev awf profile preview . --profile auto
```

## Workspace Lifecycle

The normal feature-branch task path uses these workspace states:

```text
requested
  -> provisioning
  -> ready
  -> running
  -> validating
  -> pushing
  -> monitoring_pr
  -> completed
```

Failure and operator paths:

```text
requested/provisioning/ready/running/validating/pushing/monitoring_pr
  -> failed
  -> cancelled
  -> destroying
  -> destroyed
```

What each stage means:

| State | Meaning |
| --- | --- |
| `requested` | Workspace row exists. The task is accepted by the control plane. |
| `provisioning` | AWF is preparing git worktree, profile, and workspace stack. |
| `ready` | Workspace stack is available for execution. |
| `running` | The coding agent is editing and committing inside the workspace. |
| `validating` | AWF is running profile/request validation. |
| `pushing` | AWF is pushing the task branch and creating a PR. |
| `monitoring_pr` | AWF owns the PR until it merges, closes, or fails. |
| `completed` | The PR merged or was observed as merged. Successful stack cleanup ran. |
| `failed` | A terminal failure occurred. The workspace is preserved for inspection. |
| `cancelled` | Operator or orchestrator cancelled the workspace. |
| `destroying` | Cleanup requested. |
| `destroyed` | Workspace resources removed. |

## PR Monitor Actions

`monitoring_pr` is where AWF stops being just a task runner and starts acting
like a PR owner.

The pure decision core returns one of these actions:

| Action | Meaning |
| --- | --- |
| `AddressComments` | Meaningful unresolved review threads/comments exist. AWF invokes the coding agent again to fix them. |
| `ReportCiFailure` | Checks failed. AWF gathers available failing logs and asks the agent to fix the failure. |
| `SyncBase` | The PR branch is behind or GitHub says the branch needs base synchronization. AWF merges the base into the head branch. |
| `WaitForCI` | Checks or GitHub mergeability are still pending. AWF sleeps and polls again. |
| `Merge` | All merge gates are green. AWF may merge after review grace and final settle checks. |
| `NotifyHuman` | A non-code policy blocker exists, or auto-merge is disabled. AWF posts one deduped human-attention note and keeps polling. |
| `ShortCircuitCompleted` | The PR was merged externally. AWF marks the workspace complete and cleans up. |
| `Abort` | The PR closed or an unrecoverable monitor failure occurred. |

The merge gate is intentionally conservative. A PR can merge only after:

- meaningful unresolved comments are gone or addressed,
- required and advisory checks are successful, skipped, or neutral,
- GitHub reports a mergeable branch state,
- the branch is not behind the base branch,
- the final short pre-merge settle recheck still sees the same green state.

### AddressComments

`AddressComments` is the review-fix loop.

When meaningful bot or human feedback appears, AWF:

1. Sends the thread/comment text and context to the same agent runtime.
2. Lets the agent edit and commit locally.
3. Waits a short settle interval for more comments.
4. Re-polls GitHub.
5. Repeats if more new comments arrived.
6. Pushes the accumulated fix commits.
7. Resolves fixed GitHub review threads.
8. Re-enters normal PR monitoring.

This is why AWF workspaces must stay alive after PR creation. The agent that
created the PR is also responsible for repairing it.

### Initial Review Grace

Feature PRs with `auto_merge: true` do not merge immediately on the first green
snapshot. AWF applies a one-time initial review grace window:

```yaml
monitor:
  initial_review_grace_period_seconds: 900
```

Rules:

- Default is 900 seconds.
- Task JSON can override it.
- `0` restores fast auto-merge behavior.
- The window starts when the PR first enters `monitoring_pr`.
- The window does not restart after AWF pushes fix commits.
- During the window, AWF continues polling.
- If comments arrive during the window, AWF handles them normally.
- After the window has elapsed once, future fix commits rely on normal gates:
  comments, CI, mergeability, branch freshness, and final settle recheck.

### Non-Actionable Bot Comments

AWF ignores bot status comments that are not actionable code review.

Example: CodeRabbit may post:

```text
Review skipped
Auto reviews are disabled on base/target branches other than the default branch.
```

That message should not trigger `NotifyHuman` and should not block merge. AWF
still respects the initial review grace window and still handles meaningful
review threads from Gemini, CodeRabbit, humans, or other reviewers.

## API Surface

Run the API locally:

```bash
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Health check:

```bash
curl http://localhost:8000/healthz
```

Create a v2 workspace:

```bash
curl -X POST http://localhost:8000/v2/workspaces \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: example-task-001" \
  -d '{
    "repo": {
      "url": "git@github.com:example/app.git",
      "base_branch": "main"
    },
    "task": {
      "title": "Implement feature",
      "prompt": "Build the requested feature and commit the result.",
      "kind": "feature_branch_pr",
      "agent": "codex",
      "auto_merge": true,
      "initial_review_grace_period_seconds": null
    },
    "workspace": {
      "profile_ref": "auto",
      "profile": null
    },
    "validation": {
      "commands": ["pytest -q"],
      "requested_tier": 1
    },
    "resources": {
      "cpu": 4,
      "memory": "8g"
    }
  }'
```

Current local-service behavior: the REST API persists workspace requests and
exposes state, and the always-on worker drives feature PR workspaces through the
full lifecycle: `requested -> provisioning -> ready -> running -> validating ->
pushing -> monitoring_pr -> completed/failed`. Feature PR workspaces created
through the service use the resolved profile's monitor grace window
(`monitor.initial_review_grace_period_seconds`, default `900`) unless the task
sets `initial_review_grace_period_seconds`. `auto_merge: true` routes to the
feature monitor, which may merge after the gates pass. `auto_merge: false`
routes to the manual/release monitor behavior: AWF posts the ready-for-human
comment and keeps polling until a human merge is observed. Release/sync flows
remain available through the compatibility dogfood scripts.

Get one workspace:

```bash
curl http://localhost:8000/v1/workspaces/ws_123
```

List workspaces:

```bash
curl "http://localhost:8000/v1/workspaces?limit=50"
```

List workspaces with dashboard-friendly filters:

```bash
curl "http://localhost:8000/v1/workspaces?status=monitoring_pr&agent=codex&repo_url=git@github.com:example/app.git&limit=25"
```

Poll immutable events:

```bash
curl "http://localhost:8000/v1/events?workspace_id=ws_123&limit=50"
```

Events response shape:

```json
{
  "items": [],
  "next_cursor": null,
  "has_more": false
}
```

## CLI Surface

The CLI is intentionally thin and JSON-first.

Start the API:

```bash
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Run the provisioning worker:

```bash
uv run --python 3.12 --extra dev awf worker
```

Inspect local service settings and dependency status:

```bash
uv run --python 3.12 --extra dev awf service config
uv run --python 3.12 --extra dev awf service status
uv run --python 3.12 --extra dev awf service status --format pretty
```

Inspect the local service Compose logs without writing Docker commands:

```bash
uv run --python 3.12 --extra dev awf service logs
uv run --python 3.12 --extra dev awf service logs --tail 200 --service worker
uv run --python 3.12 --extra dev awf service logs --follow --service api --service worker
```

`awf service logs` is a read-only wrapper around
`docker compose -f docker/compose/local-service.yml logs`. By default it tails
the `api` and `worker` services. Repeat `--service` to select `api`, `worker`,
`migrate`, or `postgres`.

Create a workspace:

```bash
uv run --python 3.12 --extra dev awf workspace create \
  --repo git@github.com:example/app.git \
  --base main \
  --profile auto \
  --agent codex \
  --title "Implement feature" \
  --prompt "Build the requested feature and commit the result." \
  --test "pytest -q"
```

Add `--no-auto-merge` to keep monitoring after AWF posts the ready-for-human
comment, and `--initial-review-grace-period-seconds 0` only for explicit
fast-path tests.

Show a workspace:

```bash
uv run --python 3.12 --extra dev awf workspace show ws_123
```

List workspaces:

```bash
uv run --python 3.12 --extra dev awf workspace list --limit 25
```

Pretty output:

```bash
uv run --python 3.12 --extra dev awf workspace show ws_123 --format pretty
```

Preview profile resolution:

```bash
uv run --python 3.12 --extra dev awf profile preview ~/Projects/example-repo --profile auto
```

## Local Service Stack

Docker Compose is the default local runtime for the always-on AWF control
plane. The stack runs:

- Postgres for the AWF control-plane database.
- A one-shot `migrate` service that runs Alembic before API/worker startup.
- The AWF API service.
- The AWF worker service.

The API, worker, and migration services use the local `awf-control-plane:local`
image built from `docker/control-plane.Dockerfile`. It includes Python, `uv`,
git/SSH tooling, the Docker CLI, the Docker Compose plugin, and the AWF package
itself. The default service stack does not bind-mount the repository into the
control-plane containers; rebuild the image to pick up code changes. Both the
API and worker containers mount `/var/run/docker.sock` so AWF can create,
inspect, and manage per-workspace Compose stacks on the host Docker daemon.

Because the API and worker use the host Docker daemon, AWF state must live at a
host-visible path that is mounted at the same absolute path inside the
containers. The local stack defaults this to `${HOME}/.awf/service`, overridable
with `AWF_HOST_WORK_DIR=/absolute/path`.

The worker also needs host-visible credential paths so workspace stacks can bind
the same auth into agent containers. Local service mode mounts only the known
credential paths under `${AWF_HOST_HOME:-${HOME}}` into the API and worker
containers, rather than mounting the whole home directory. It also forwards
Docker Desktop's `/run/host-services/ssh-auth.sock` so service-worker Git
operations can use the operator's loaded SSH keys. Set `AWF_HOST_HOME` if the
shell running Docker Compose does not expose the operator home as `${HOME}`.

The worker reuses the per-workspace Compose file and project created during
provisioning. It does not launch a second stack for agent execution,
validation, PR creation, or PR monitoring; those stages run against the same
workspace services and keep agent, validation, and monitor logs in the durable
workspace log store.

Start from a clean checkout:

```bash
cp .env.example .env
docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
docker compose -f docker/compose/local-service.yml up --build
```

Check the service from another terminal:

```bash
uv run --python 3.12 --extra dev awf service status
uv run --python 3.12 --extra dev awf service logs --follow --service worker
```

The service-mode default database URL is local Postgres
(`postgresql+asyncpg://awf:...@localhost:5433/awf`). SQLite remains supported
for tests and throwaway script runs, but the always-on service should run
against Postgres.

The local Compose stack defaults `AWF_API_TOKEN` to `local-dev-token`. Use the
same value in the console `.env.local` file, or override it consistently in the
shell before starting the stack.

The AWF Postgres database is only the control-plane database. Project and
workspace databases remain separate and profile-isolated; if a workspace
profile needs Postgres or another datastore, that service belongs to the
per-workspace Compose stack, not the AWF control-plane DB.

## MCP Surface

AWF also exposes MCP tools for clients that want typed tool calls instead of
shelling out to the REST API:

| Tool | Purpose |
| --- | --- |
| `awf_create_workspace` | Create a legacy v1 workspace request. |
| `awf_create_workspace_v2` | Create a profile-driven v2 workspace request. |
| `awf_get_workspace` | Fetch one workspace by id. |
| `awf_list_workspaces` | List recent workspaces newest-first. |
| `awf_wait_for_workspace` | Poll until a workspace reaches a terminal state or times out. |
| `awf_get_workspace_runtime` | Fetch one workspace's compose/container runtime snapshot. |
| `awf_list_workspace_operations` | List one workspace's active and completed operations newest-first. |
| `awf_list_workspace_events` | List one workspace's immutable events newest-first, with optional event-type filtering. |
| `awf_list_workspace_logs` | List indexed durable log streams for one workspace. |
| `awf_read_workspace_log` | Read a bounded log chunk by stream id and byte offset. |

The observability tools return `null` for a missing workspace or log stream
rather than surfacing raw storage errors. Runtime and operation tools are
read-only; operator controls such as cancel/destroy stay on the authenticated
REST API.

Example `awf_create_workspace_v2` arguments:

```json
{
  "repo_url": "git@github.com:example/app.git",
  "base_branch": "main",
  "task_title": "Implement feature",
  "task_prompt": "Build the requested feature and commit the result.",
  "task_kind": "feature_branch_pr",
  "agent": "codex",
  "task_external_id": "AIRA-123",
  "profile_ref": "auto",
  "profile": null,
  "validation_commands": ["pytest -q"],
  "requested_tier": 1,
  "auto_merge": true,
  "initial_review_grace_period_seconds": null
}
```

Example runtime and operation observability calls:

`awf_get_workspace_runtime` arguments:

```json
{"workspace_id": "ws_abc123"}
```

`awf_list_workspace_operations` arguments:

```json
{"workspace_id": "ws_abc123", "limit": 25}
```

## Local Dogfood Runner

`scripts/run_awf.py` is the compatibility dogfood runner for exercising the
same building blocks outside the always-on service. It creates a local SQLite
control-plane database under a run directory, provisions workspaces, launches
Docker Compose, runs the agent, creates a PR, and runs the PR monitor. The
service worker is now the normal always-on feature PR executor; use the script
for isolated experiments, checked-in task specs, and release/sync flows.

Example config:

```json
[
  {
    "repo_url": "git@github.com:dimileeh/aira-agent-workspace-fabric.git",
    "branch_base": "codex/awf-universal-profile-base",
    "task_title": "Add workspace list filters for operator console",
    "task_prompt": "Implement the requested feature with tests.",
    "agent": "codex",
    "test_commands": [
      "uv run ruff check src/awf/api src/awf/db tests/unit/api tests/unit/db",
      "uv run mypy src/awf/api src/awf/db",
      "uv run pytest tests/unit/api tests/unit/db/test_workspace_repository.py -q"
    ],
    "requires_database": false,
    "profile_ref": "auto",
    "task_kind": "feature_branch_pr",
    "auto_merge": true,
    "initial_review_grace_period_seconds": 900
  }
]
```

`auto_merge: true` means the feature PR monitor may merge after all gates pass.
`initial_review_grace_period_seconds: 0` is useful only for explicit fast-path
tests; normal dogfood runs should keep the default grace window so first-pass
reviewers have time to post comments.

Run it:

```bash
uv run --python 3.12 --extra dev python scripts/run_awf.py \
  --config /tmp/awf-task.json \
  --work-dir ~/.awf/runs/example-run
```

Run state is preserved by default. Reusing the same `--work-dir` appends new
workspace rows to the existing `awf.db`, which keeps the API, PR monitors, and
console looking at one consistent run history.

Reset a throwaway run database only when no API or monitor process is using it:

```bash
uv run --python 3.12 --extra dev python scripts/run_awf.py \
  --config /tmp/awf-task.json \
  --work-dir ~/.awf/runs/example-run \
  --reset-state
```

## Setup

### Prerequisites

Install:

- Python 3.12.
- `uv`.
- Docker Desktop or Docker Engine with Compose plugin.
- Git.
- GitHub CLI `gh`.
- A GitHub account with access to the target repo.
- SSH key or Git credentials that can clone and push the repo.
- At least one coding-agent credential:
  - Codex CLI auth in `~/.codex`, or OpenAI auth environment as supported by
    the installed Codex CLI.
  - Claude Code auth in `~/.claude` / `~/.claude.json` or Anthropic env vars.
  - Gemini auth in `~/.gemini` or Google/Gemini env vars.

Verify GitHub CLI:

```bash
gh auth status
```

Verify Docker:

```bash
docker info
docker compose version
```

### Clone and Install

```bash
git clone git@github.com:dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric

uv sync --extra dev
```

Run tests:

```bash
uv run --python 3.12 --extra dev pytest -q
```

Run lint and types:

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev ruff format --check .
uv run --python 3.12 --extra dev mypy
```

### Build the Agent Runtime Image

AWF workspaces use `awf-agent-runtime:latest` unless configured otherwise.

```bash
docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
```

Verify:

```bash
docker image inspect awf-agent-runtime:latest
```

### Configure Environment

Local service development should use Postgres via the Compose stack:

```bash
cp .env.example .env
export AWF_GITHUB_TOKEN="$(gh auth token)"
docker compose -f docker/compose/local-service.yml up --build
```

For API-only throwaway development, SQLite remains supported:

```bash
export AWF_DATABASE_URL="sqlite+aiosqlite:///./awf.db"
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Key local service values:

```text
AWF_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@localhost:5433/awf
AWF_API_TOKEN=local-dev-token
AWF_AGENT_RUNTIME_IMAGE=awf-agent-runtime:latest
AWF_HOST_WORK_DIR=${HOME}/.awf/service
AWF_HOST_HOME=${HOME}
AWF_GITHUB_TOKEN=<token from gh auth token>
```

The local dogfood runner uses its own SQLite DB under `--work-dir`, so it does
not require a separate Postgres control-plane database.

### Agent Credentials in Containers

`scripts/run_awf.py` and local service worker-created workspace stacks map local
auth into the agent container:

- `~/.config/gh`
- `~/.config/gcloud`
- `~/.gitconfig`
- `~/.ssh`
- `~/.codex` copied into a per-workspace isolated auth directory.
- `~/.claude` and `~/.claude.json`
- `~/.gemini`
- selected provider environment variables.

Codex auth is intentionally isolated per workspace because a live host
`~/.codex` contains state and locks that can collide with Codex Desktop.

For local service mode, these host paths must be visible to the worker at their
host absolute paths. `docker/compose/local-service.yml` does this by mounting
only the listed credential paths read-only into the control-plane containers;
the worker copies only Codex `auth.json`, `config.toml`, `installation_id`, and
`rules/` into `${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/codex`
before launching the workspace stack.

Default agent models and effort are centralized in
`src/awf/adapters/defaults.py`:

| Agent | Default model | AWF effort |
| --- | --- | --- |
| `claude_code` | `claude-opus-4-7` | `xhigh` mapped to Claude Code `max` |
| `codex` | `gpt-5.5` | `xhigh` via `model_reasoning_effort` |
| `gemini` | `gemini-3.1-pro` | `xhigh` mapped to Gemini `HIGH` thinking |

If a local subscription or provider account cannot use a default model, choose a
supported model in the task or adapter configuration. For example, Gemini
dogfood tests can use a Flash preview model when Pro is unavailable.

### Database Migrations

SQLite local API runs create tables automatically at startup. For Postgres,
apply migrations before starting the API and worker. The Compose stack does
this through its `migrate` service:

```bash
docker compose -f docker/compose/local-service.yml up migrate
```

Manual Postgres migration:

```bash
AWF_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@localhost:5433/awf \
  uv run --python 3.12 --extra dev alembic upgrade head
```

### Run the API Server

```bash
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Open API docs:

```text
http://localhost:8000/docs
```

### Run a Full Local AWF Task

1. Ensure Docker is running.
2. Ensure `awf-agent-runtime:latest` exists.
3. Ensure `gh auth status` is clean.
4. Ensure the target repo can be cloned and pushed over SSH.
5. Write a `scripts/run_awf.py` JSON task.
6. Run `scripts/run_awf.py`.
7. Watch the terminal logs for:
   - `agent.run.start`
   - `agent.run.ok`
   - `pr.created`
   - `monitor.action`
   - `monitor.initial_review_grace_waiting`
   - `monitor.compose_teardown_ok`

## Stranded PR Watchdog

The monitor is supposed to stay alive until the PR merges, closes, or fails. If
the host restarts, Docker dies, or a process is killed, an open `awf/` feature
PR can be stranded with no process reading new comments.

Run one watchdog per host:

```bash
uv run --python 3.12 --extra dev awf-watchdog \
  --work-dir ~/.awf/runs \
  --poll-seconds 300 \
  --repo dimileeh/aira-agent-workspace-fabric
```

The watchdog lists open `awf/` PRs with `gh pr list`, checks whether a matching
`run_awf.py` monitor process is already running, and calls
`scripts/attach_feature_pr_monitor.py` for any stranded PR. The attach script is
idempotent and uses a file lock so repeated watchdog scans do not double-spawn a
monitor for the same PR.

## Working With Dockerized Projects

For non-Docker projects, profile `docker.mode` can be `none`.

For Dockerized projects, use profile `docker.mode: dind`. AWF then launches a
per-workspace Docker daemon sidecar. The agent receives:

```text
DOCKER_HOST=tcp://docker:2375
```

Project containers started by profile phases run inside that per-workspace
daemon. This keeps project Docker Compose stacks isolated from the host and
from other AWF workspaces.

The agent may use Docker for diagnostics and tests, but AWF remains the
lifecycle authority for workspace services.

Minimal repo-local Docker profile:

```yaml
awf:
  name: my-compose-app
  version: 1
  docker:
    mode: dind
  runtime:
    environment:
      DOCKER_HOST: tcp://docker:2375
      APP_BASE_URL: http://docker:3000
  phases:
    setup:
      - command: docker compose up -d --wait
        timeout_seconds: 600
    validate:
      - command: docker compose exec -T app pytest -q
        timeout_seconds: 600
    cleanup:
      - command: docker compose down -v --remove-orphans
        timeout_seconds: 300
```

## Observability

AWF includes a local Next.js console under `apps/console`. It talks to AWF
through Next.js BFF routes, so `AWF_API_TOKEN` stays server-side and is never
sent to browser JavaScript.

Start the full local service stack, which sets `AWF_API_TOKEN=local-dev-token`
by default:

```bash
docker compose -f docker/compose/local-service.yml up --build
```

For API-only throwaway development, start the AWF API with a matching local
token:

```bash
AWF_API_TOKEN=local-dev-token uv run --python 3.12 --extra dev awf serve --reload
```

Then start the console:

```bash
cd apps/console
cp .env.example .env.local
npm install
npm run dev
```

The console uses these AWF endpoints:

- `GET /v1/tasks` and `GET /v1/workspaces/overview` for the workspace list.
- `GET /v1/workspaces/{id}` for selected workspace details.
- `GET /v1/workspaces/{id}/runtime` for compose/container state.
- `GET /v1/workspaces/{id}/events` for the workspace timeline.
- `GET /v1/workspaces/{id}/operations` for active and completed operations.
- `GET /v1/workspaces/{id}/logs` and `GET /v1/workspaces/{id}/logs/{stream_id}` for log metadata and tail reads.
- `WebSocket /v1/workspaces/{id}/ws`, proxied as browser-safe SSE at `/api/awf/workspaces/{id}/stream`, for live events and log frames.

Recent dogfood observability slices added:

- `/v1/events` and `/v1/workspaces/{id}/events`.
- `/v1/workspaces` filters by status, agent, and repo URL.
- Durable agent, validation, and service log streams.
- Runtime, operation, and workspace control endpoints.

## Failure Handling

AWF uses coarse failure reasons today:

- `agent_failure`
- `validation_failure`
- `infrastructure_failure`
- `policy_failure`
- `cleanup_failure`
- `profile_resolution_failure`
- `service_startup_failure`
- `phase_timeout`
- `health_check_failure`

Successful workspaces are torn down after completion. Failed workspaces are
preserved so an operator can inspect containers, logs, worktrees, and artifacts.

## Development Workflow

Before pushing changes:

```bash
uv run --python 3.12 --extra dev ruff check .
uv run --python 3.12 --extra dev ruff format --check .
uv run --python 3.12 --extra dev mypy
uv run --python 3.12 --extra dev pytest -q
```

Useful focused commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api -q
uv run --python 3.12 --extra dev pytest tests/unit/runtime -q
uv run --python 3.12 --extra dev pytest tests/unit/common/test_github_client.py -q
```

## Glossary

| Term | Meaning |
| --- | --- |
| AWF | Aira Agent Workspace Fabric. |
| Workspace | One isolated task execution environment and its persisted control-plane row. |
| Profile | Project-specific runtime, services, phases, validation, secrets, and monitor policy. |
| Agent runtime | The coding CLI launched inside the workspace container. |
| PR monitor | Per-workspace loop that owns a PR through comments, CI, base sync, and merge. |
| `AddressComments` | PR monitor action that asks the agent to fix meaningful review feedback. |
| `NotifyHuman` | PR monitor action for manual-merge mode or non-code policy blockers. |
| Initial review grace | One-time wait after PR monitoring starts before auto-merge may happen. |
| DinD | Docker-in-Docker sidecar used for Dockerized projects. |

## License

Apache-2.0. See [LICENSE](LICENSE).
