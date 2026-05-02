# Aira Agent Workspace Fabric (AWF)

**AWF is an industrial workspace fabric for AI coding agents.**

It gives Codex, Claude Code, Gemini, and future coding agents a repeatable way
to work like disciplined software contributors: each task gets an isolated
workspace, a clean checkout, declared services, validation, PR creation, PR
review monitoring, comment-fix loops, merge gates, artifacts, events, and
cleanup.

AWF is not a chatbot and not a product-planning brain. It is the execution
substrate beneath a planner such as Aira, a human operator, or an MCP client;
inside a workspace it can enforce a concrete implementation-plan lifecycle.

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
6. Optionally run AWF-owned Plan -> Execute -> Compare iterations.
7. Run the selected coding agent inside the workspace container.
8. Run profile validation phases and explicit request validation commands.
9. Commit, push, and open a pull request.
10. Monitor the PR until it is merged, closed, or failed.
11. Address meaningful review comments by invoking the same agent again.
12. Fix CI failures when logs are available.
13. Sync the base branch into the PR branch when needed.
14. Respect reviewer timing through an initial review grace window.
15. Auto-merge only after all gates pass.
16. Tear down successful workspaces and preserve failed ones for inspection.

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
- Codex, Claude Code, Gemini, and OpenCode adapters.
- Central default model/effort map for agent adapters.
- AWF-owned Plan -> Execute -> Compare lifecycle policy.
- Generic phase-based validation.
- Git worktree provisioning.
- PR creation.
- Feature PR monitor with automated comment handling and auto-merge.
- Release/sync PR monitor variants that keep workspaces alive until human merge.
- Post-merge target-branch reconciliation for Python/Alembic multi-head repair.
- Initial PR review grace period before auto-merge.
- Durable v2 task policy metadata (`task_class`, `owned_paths`) for later lock scheduling.
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
- Full secret lease broker (local profiles declare security/egress and secrets metadata, but full cloud enforcement is pending).

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
- `planning`: optional Plan -> Execute -> Compare policy and artifact paths.
- `validation`: health checks, artifact paths, timeout and tier hints.
- `monitor`: PR-monitor policy such as initial review grace.
- `secrets`: named mount or env leases. In local mode, declare explicit
  `provider: env`, `provider: github`, `provider: host-file`, or
  `provider: local-auth` refs instead of broad host-home mounts.
- `security`: local egress policy, related security declarations, and profile lint policy for credential mounts.
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
| `go` | Detects `go.mod`; downloads modules and runs `go test ./...`. |
| `rust` | Detects `Cargo.toml`; fetches crates and runs `cargo test --all-targets`. |
| `java` | Detects `mvnw`, `pom.xml`, `build.gradle`, or `gradlew`; uses Maven or Gradle test defaults. |
| `cpp` | Detects `CMakeLists.txt`; configures with CMake, builds, then runs CTest. |
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
  planning:
    required: true
    plan_path: docs/awf-plans/{workspace_id}.md
    conformance_report_path: docs/awf-plans/{workspace_id}.conformance.json
    max_iterations: 2
    enforce_plan_only_changes: true
    fail_on_unexplained_deviation: true
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

When `planning.required` is true, AWF owns a provider-neutral planning lifecycle
instead of relying on an agent-specific interactive plan mode:

1. Ask the agent to write a plan artifact and refuse planning-phase code changes.
2. Ask the agent to implement the saved plan.
3. Ask the agent to write a structured conformance report.
4. Iterate execution while the report says plan gaps remain.
5. Fail the workspace if the plan is not satisfied within the configured budget.

This works the same way for Codex, Claude Code, Gemini, and future adapters
because the control plane invokes normal non-interactive agent runs for each
phase and stores the plan/report inside the workspace.

Preview the profile AWF would resolve for a checkout:

```bash
uv run --python 3.12 --extra dev awf profile preview . --profile auto
```

### Local egress policy

Workspace profiles can declare `security.egress`, and local Docker mode enforces
the subset that Compose can represent safely:

| Mode | Local Docker behavior |
| --- | --- |
| `restricted` | Default. The workspace Compose network renders `internal: true`, and the agent host-gateway mapping is omitted. Destination-level filtering is not implemented in this local slice. |
| `offline` | The workspace Compose network renders `internal: true`, and the agent host-gateway mapping is omitted. Profile services remain reachable on `awf_net`. |
| `open` | Explicit trusted-local posture. The workspace network remains public and the agent keeps `host.docker.internal:host-gateway`, which is unrestricted internet access. |

Legacy `allowlist` and `mirrored` egress modes are rejected by profile schema
validation. Destination-level proxy/firewall audit and allowlisting are a
separate future security slice.

This local slice does not implement cloud `NetworkPolicy`, host firewall rules,
iptables/nftables mutation, DNS filtering, transparent proxies, or package
registry mirror management.

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
| `completed` | The PR merged or was observed as merged. Compose teardown ran best-effort; filesystem pressure-dir cleanup is retention-based. |
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
      "model": null,
      "task_class": "refactor_task",
      "owned_paths": ["src/**", "tests/**"],
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

The v2 task object also accepts policy metadata for future deterministic
scheduling:

- `task_class`: optional; one of `docs_task`, `test_task`, `refactor_task`,
  `migration_task`, `dependency_task`, or `build_config_task`.
- `owned_paths`: optional list of path globs/strings the task expects to own;
  omitted values default to `[]`.

AWF persists and returns these fields on workspace, task, overview, and MCP
workspace create/get/list responses. This slice does not enforce locks or
change scheduling behavior yet.

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

List and download workspace artifacts through the protected observability API:

```bash
curl -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/artifacts"

curl -OJ -H "Authorization: Bearer $AWF_API_TOKEN" \
  "http://localhost:8000/v1/workspaces/ws_123/artifacts/download?path=logs/stdout.txt"
```

Artifact downloads are limited to regular files under
`<AWF_WORK_DIR>/artifacts/<workspace_id>` using POSIX-style relative paths.
Absolute paths, traversal segments, backslashes, symlinks, and missing files are
rejected without reading arbitrary host paths.

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

Local service mode uses a stable worker node id, `local`, so active rows
survive API/worker/migrate container rebuilds without becoming owned by a dead
container hostname. Multi-node deployments should set a unique
`AWF_WORKER_NODE_ID` per node; stale active-execution recovery remains scoped to
the current node id and does not recover rows owned by unrelated nodes.

Inspect local service settings and dependency status:

```bash
uv run --python 3.12 --extra dev awf service config
uv run --python 3.12 --extra dev awf service status
uv run --python 3.12 --extra dev awf service status --format pretty
```

`awf service status` reports `orphan_workspaces` and `workspace_cleanup` checks
alongside the existing API / DB / Docker / image / disk checks. It reads
Docker Compose labels for containers, networks, and volumes, and scans
`<work_dir>/git/worktrees/ws_*` for managed worktrees. Resources for active
workspaces are expected; completed workspaces still inside the service GC
retention window are reported as retained instead of unsafe. Resources tied to
missing workspace rows or terminal rows past retention are reported with
structured counts, examples, reason codes, and suggested follow-up actions.
The check returns structured `unavailable`/`unknown` warnings (rather than
raising) when Docker or the database is offline.

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

Run one target-branch reconciliation pass:

```bash
uv run --python 3.12 --extra dev awf service reconcile-target \
  --repo-url git@github.com:owner/repo.git \
  --branch development
```

The service worker also invokes this reconciliation hook after a monitored PR
reaches `completed`. The first resolver is Python/Alembic-specific: if several
merged workspace PRs leave the integrated target branch with multiple Alembic
heads, AWF writes an empty Alembic merge revision and pushes it as a follow-up
commit to the target branch. Use `--dry-run` to inspect the resolver result
without committing or pushing.

Plan terminal workspace filesystem garbage collection:

```bash
uv run --python 3.12 --extra dev awf service gc
uv run --python 3.12 --extra dev awf service gc --format pretty
uv run --python 3.12 --extra dev awf service gc --min-age-hours 336 --limit 20
```

`awf service gc` defaults to a dry-run JSON plan. Without `--status` filters it
selects only completed PR workspaces whose retention window has expired
(`AWF_COMPLETED_WORKSPACE_RETENTION_HOURS`, default `168`). Recent completed PR
workspaces and failed workspaces are reported in the `preserved` section with
reason codes such as `WORKSPACE_WITHIN_RETENTION` and
`FAILED_WORKSPACE_TRIAGE_PRESERVED`. Use `--retention-hours` or the compatible
`--min-age-hours` flag to override the retention window for one run. Each
candidate reports the worktree, compose, and auth paths plus estimated bytes;
missing paths are reported as zero bytes.

Execute the same filesystem-only cleanup with:

```bash
uv run --python 3.12 --extra dev awf service gc --execute
```

Execution deletes only `<work_dir>/git/worktrees/<workspace>`,
`<work_dir>/compose/<workspace>` or the stored compose-file parent, and
`<work_dir>/auth/<workspace>`. It does not delete control-plane database rows,
workspace events, log streams, or files under `<work_dir>/logs` and
`<work_dir>/artifacts`; durable logs and artifacts remain available for audit
and postmortem inspection. Repeated runs are idempotent: missing pressure
directories are reported as `already_removed`, and partial failures return a
structured `partial` result with reason codes instead of deleting unsafe paths.

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

Inspect workspace observability data:

```bash
uv run --python 3.12 --extra dev awf workspace events ws_123 --limit 50
uv run --python 3.12 --extra dev awf workspace events ws_123 --event-type workspace.created
uv run --python 3.12 --extra dev awf workspace runtime ws_123
uv run --python 3.12 --extra dev awf workspace operations ws_123 --limit 25
uv run --python 3.12 --extra dev awf workspace logs ws_123
uv run --python 3.12 --extra dev awf workspace log ws_123 agent.stdout --offset 0 --limit-bytes 65536
```

For protected observability endpoints, set `AWF_API_TOKEN` or pass
`--api-token` on the command. The CLI sends it as a bearer token and never
prints it.

Pretty output:

```bash
uv run --python 3.12 --extra dev awf workspace show ws_123 --format pretty
uv run --python 3.12 --extra dev awf workspace events ws_123 --format pretty
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
the same auth into agent containers. The preferred path is profile-declared
local secret leases under `secrets`: env leases render only Compose
placeholders, and mount leases bind exact read-only files or known local auth
paths. Local service mode still mounts only the known credential paths under
`${AWF_HOST_HOME:-${HOME}}` into the API and worker containers so legacy
providers that need per-workspace writable copies can be seeded. It does not
mount the whole home directory. It also forwards Docker Desktop's
`/run/host-services/ssh-auth.sock` so service-worker Git operations can use the
operator's loaded SSH keys. Set `AWF_HOST_HOME` if the shell running Docker
Compose does not expose the operator home as `${HOME}`.

Credential values used by Compose interpolation must be present in the shell
that starts the stack or in a Compose env file such as `docker/compose/.env`.
The repo-root `.env` is still useful for Python `awf` commands, but the
control-plane containers only see values that Docker Compose injects. On macOS,
`gh` auth stored only in Keychain-backed `~/.config/gh` is not usable inside the
containers; set `AWF_GITHUB_TOKEN` or `GH_TOKEN`, commonly from
`gh auth token`, before starting the stack.

The worker reuses the per-workspace Compose file and project created during
provisioning. It does not launch a second stack for agent execution,
validation, PR creation, or PR monitoring; those stages run against the same
workspace services and keep agent, validation, and monitor logs in the durable
workspace log store.

Start from a clean checkout with the repeatable bootstrap command:

```bash
cp .env.example .env
uv run --python 3.12 --extra dev awf service bootstrap
uv run --python 3.12 --extra dev awf service status --format pretty
```

`awf service bootstrap` builds the configured agent runtime image, starts
Postgres, reruns the Compose `migrate` service, starts the API and worker, and
polls `awf service status` until the service is healthy. It is safe to re-run
when containers and volumes already exist; the migration service is force
recreated and Alembic upgrades are idempotent.

The lower-level Compose workflow remains supported:

```bash
docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
docker compose -f docker/compose/local-service.yml up --build
```

Inspect the service and logs from another terminal:

```bash
uv run --python 3.12 --extra dev awf service status
uv run --python 3.12 --extra dev awf service status --provider github --format pretty
curl 'http://localhost:8000/readyz?provider=github'
uv run --python 3.12 --extra dev awf service logs --follow --service worker
```

`awf service status` and `/readyz` include an `agent_readiness` section for
GitHub, Codex, Claude Code, Gemini, OpenCode/Ollama, and Docker. Each provider
reports redacted `credential_sources`, `credential_scope`, `isolation`, and
structured warnings. Missing optional providers and local least-privilege
downgrades are warnings by default. Pass `--provider <name>` or
`?provider=<name>` to make that provider strict for scheduling or rollout
checks.

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

### Local Service Image Versioning

The local service uses mutable Docker tags for fast iteration:
`awf-control-plane:local` for API, worker, and migrations, and
`awf-agent-runtime:latest` for agent workspaces. Before a local upgrade, record
the source revision and image IDs so an image rollback has a concrete target:

```bash
git rev-parse --short HEAD
docker image inspect awf-control-plane:local
docker image inspect awf-agent-runtime:latest
```

When you want named local rollback anchors, tag both images with the same
version label after building them:

```bash
export AWF_LOCAL_VERSION="$(git rev-parse --short HEAD)"
docker compose -f docker/compose/local-service.yml build
docker tag awf-control-plane:local "awf-control-plane:${AWF_LOCAL_VERSION}"
docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
docker tag awf-agent-runtime:latest "awf-agent-runtime:${AWF_LOCAL_VERSION}"
docker image inspect "awf-control-plane:${AWF_LOCAL_VERSION}"
docker image inspect "awf-agent-runtime:${AWF_LOCAL_VERSION}"
```

The Compose stack still points at `awf-control-plane:local` by default, and
workspace execution still points at `awf-agent-runtime:latest` unless
`AWF_AGENT_RUNTIME_IMAGE` is overridden. The extra local tags are operator
bookmarks for verification and rollback, not a registry release scheme.

### Local Service Upgrade

For a normal local upgrade, capture a pre-upgrade backup, rebuild both images,
rerun migrations, and check health:

```bash
export AWF_HOST_WORK_DIR="${AWF_HOST_WORK_DIR:-$HOME/.awf/service}"
mkdir -p "$AWF_HOST_WORK_DIR/backups"
docker compose -f docker/compose/local-service.yml up -d postgres
docker compose -f docker/compose/local-service.yml exec -T postgres \
  pg_dump -U awf -d awf -Fc \
  > "$AWF_HOST_WORK_DIR/backups/awf-control-plane-pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
docker compose -f docker/compose/local-service.yml build
uv run --python 3.12 --extra dev awf service bootstrap
uv run --python 3.12 --extra dev awf service status --format pretty
```

`awf service bootstrap` is the preferred upgrade path because it rebuilds the
agent runtime image, starts Postgres, force recreates the Compose `migrate`
service, starts the API and worker, and polls health. If migration startup
fails, inspect the migration logs before changing volumes or state:

```bash
uv run --python 3.12 --extra dev awf service logs --service migrate --tail 200
uv run --python 3.12 --extra dev awf service logs --service api --tail 200
uv run --python 3.12 --extra dev awf service logs --service worker --tail 200
```

### Control-Plane Postgres Backup And Restore

These commands back up and restore only the AWF control-plane database in the
local Compose `postgres` service.
They do not back up workspace or project databases, cloned worktrees,
per-workspace artifacts, or external services.

Capture a custom-format backup into the service work directory:

```bash
export AWF_HOST_WORK_DIR="${AWF_HOST_WORK_DIR:-$HOME/.awf/service}"
mkdir -p "$AWF_HOST_WORK_DIR/backups"
docker compose -f docker/compose/local-service.yml up -d postgres
docker compose -f docker/compose/local-service.yml exec -T postgres \
  pg_dump -U awf -d awf -Fc \
  > "$AWF_HOST_WORK_DIR/backups/awf-control-plane-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Restore only when the API and worker are stopped. This avoids live writes
during restore and makes the backup the single source of control-plane truth.
Before restore, stop API and worker.

```bash
export AWF_BACKUP="$HOME/.awf/service/backups/awf-control-plane-YYYYmmddTHHMMSSZ.dump"
docker compose -f docker/compose/local-service.yml stop api worker
docker compose -f docker/compose/local-service.yml up -d postgres
docker compose -f docker/compose/local-service.yml exec -T postgres \
  dropdb -U awf --maintenance-db=postgres --if-exists awf
docker compose -f docker/compose/local-service.yml exec -T postgres \
  createdb -U awf --maintenance-db=postgres awf
docker compose -f docker/compose/local-service.yml exec -T postgres \
  pg_restore -U awf -d awf --no-owner < "$AWF_BACKUP"
docker compose -f docker/compose/local-service.yml up --build --force-recreate migrate
docker compose -f docker/compose/local-service.yml up -d api worker
uv run --python 3.12 --extra dev awf service status --format pretty
```

Run `awf service status` after restore, then inspect recent logs if readiness
does not come back cleanly.

### Local Service Rollback

Rollback has two separate parts: image rollback and database migration
rollback. Image rollback can retag a previously recorded local image, but
database migration rollback is not automatically reversible. Always keep a
pre-upgrade backup before running migrations from a newer checkout.

To roll back images to a saved local version:

```bash
export AWF_ROLLBACK_VERSION=<previous-git-sha-or-local-label>
docker tag "awf-control-plane:${AWF_ROLLBACK_VERSION}" awf-control-plane:local
docker tag "awf-agent-runtime:${AWF_ROLLBACK_VERSION}" awf-agent-runtime:latest
docker compose -f docker/compose/local-service.yml up -d --force-recreate api worker
uv run --python 3.12 --extra dev awf service status --format pretty
```

If the failed upgrade already ran migrations, treat rollback as a restore from
the pre-upgrade backup. Check migration logs first.
Warning: do not delete the Postgres volume until a fresh backup has been
captured from whatever state is still readable:

```bash
uv run --python 3.12 --extra dev awf service logs --service migrate --tail 200
```

Then use the restore flow in
`Control-Plane Postgres Backup And Restore` and restart through
`awf service bootstrap`.

### Local Disaster Recovery

For stuck Compose containers, first collect state and logs, then remove only
containers and networks. The default cleanup command below intentionally does
not remove the Postgres volume:

```bash
docker compose -f docker/compose/local-service.yml ps
uv run --python 3.12 --extra dev awf service logs --tail 200
docker compose -f docker/compose/local-service.yml stop api worker migrate
docker compose -f docker/compose/local-service.yml down --remove-orphans
uv run --python 3.12 --extra dev awf service bootstrap
uv run --python 3.12 --extra dev awf service status --format pretty
```

Use `down --volumes` only as a last resort after a verified control-plane
backup exists. Removing the Compose volume destroys the local AWF
control-plane database.

For a corrupt `${AWF_HOST_WORK_DIR}`, quarantine the directory and rebuild a
clean one. Preserve logs, artifacts, backups, and auth when they are still
readable:

```bash
export AWF_HOST_WORK_DIR="${AWF_HOST_WORK_DIR:-$HOME/.awf/service}"
export AWF_QUARANTINE="${AWF_HOST_WORK_DIR}.quarantine.$(date -u +%Y%m%dT%H%M%SZ)"
docker compose -f docker/compose/local-service.yml stop api worker
mv "$AWF_HOST_WORK_DIR" "$AWF_QUARANTINE"
mkdir -p "$AWF_HOST_WORK_DIR"
for name in backups logs artifacts auth; do
  if [ -d "$AWF_QUARANTINE/$name" ]; then
    mkdir -p "$AWF_HOST_WORK_DIR/$name"
    cp -a "$AWF_QUARANTINE/$name/." "$AWF_HOST_WORK_DIR/$name/"
  fi
done
uv run --python 3.12 --extra dev awf service bootstrap
uv run --python 3.12 --extra dev awf service status --format pretty
```

If the work-dir corruption came from a partially deleted workspace, prefer
`awf service gc` after the service is healthy instead of manually removing
workspace state. If Postgres itself is suspect, capture a backup before any
destructive cleanup, then use the restore flow above.

## MCP Surface

AWF also exposes MCP tools for clients that want typed tool calls instead of
shelling out to the REST API. REST is canonical, the CLI is a JSON-first
operator convenience layer, and MCP is a first-class parity client for agent
orchestrators. See [docs/MCP_CLIENT_PARITY.md](docs/MCP_CLIENT_PARITY.md) for
the API/CLI/MCP parity matrix and explicit MCP backlog surfaces.

| Tool | Purpose |
| --- | --- |
| `awf_create_workspace` | Create a legacy v1 workspace request. |
| `awf_create_workspace_v2` | Create a profile-driven v2 workspace request. |
| `awf_get_workspace` | Fetch one workspace by id. |
| `awf_list_workspaces` | List recent workspaces newest-first. |
| `awf_wait_for_workspace` | Poll until a workspace reaches a terminal state or times out. |
| `awf_get_workspace_runtime` | Fetch one workspace's compose/container runtime snapshot. |
| `awf_list_merge_queue` | List the REST merge queue envelope for operator review. |
| `awf_list_workspace_overview` | List the REST workspace overview envelope. |
| `awf_list_workspace_validation` | List validation provenance for one workspace. |
| `awf_list_workspace_stale_reasons` | List active or resolved stale reasons for one workspace. |
| `awf_list_workspace_artifacts` | List workspace artifact metadata without reading artifact contents. |
| `awf_get_failure_analysis_summary` | Fetch the failure-analysis metrics summary. |
| `awf_get_workspace_reliability_summary` | Fetch the workspace reliability metrics summary. |
| `awf_get_resource_saturation_summary` | Fetch resource saturation, cleanup readiness, and admission status. |
| `awf_get_slo_metrics_summary` | Fetch the SLO metrics summary. |
| `awf_list_operations` | List operations globally with REST-compatible filters. |
| `awf_get_operation` | Fetch one operation by id. |
| `awf_list_workspace_operations` | List one workspace's active and completed operations newest-first. |
| `awf_list_workspace_events` | List one workspace's immutable events newest-first, with optional event-type filtering. |
| `awf_list_workspace_logs` | List indexed durable log streams for one workspace. |
| `awf_read_workspace_log` | Read a bounded log chunk by stream id and byte offset. |
| `awf_get_overlap_graph` | Fetch the advisory owned-path overlap graph. |
| `awf_list_tasks` | List task records backed by workspace attempts. |
| `awf_list_task_attempts` | List attempts for one task reference. |
| `awf_list_locks` | List owned-path reservations and overlap-risk summaries. |
| `awf_get_service_readiness` | Fetch service readiness checks. |
| `awf_get_service_health` | Fetch service liveness. |
| `awf_cancel_workspace` | Operator control: request cancellation for a workspace. |
| `awf_stop_workspace` | Operator control: stop a workspace stack. |
| `awf_destroy_workspace` | Operator control: destroy AWF-managed workspace resources. |
| `awf_remonitor_workspace` | Operator control: request PR monitor recovery. |
| `awf_request_workspace_validation` | Operator control: request workspace re-validation. |

The observability tools return `null` for a missing workspace, log stream, or
operation rather than surfacing raw storage errors. Operator observability tools
are read-only and mirror REST response envelopes; the explicit control tools do
not provide shell access or arbitrary Docker execution. Known MCP parity backlog
is documented in the matrix, including refresh, rebase, retry, artifact
content/download, and `If-Match` concurrency coverage.

Example `awf_create_workspace_v2` arguments:

```json
{
  "repo_url": "git@github.com:example/app.git",
  "base_branch": "main",
  "task_title": "Implement feature",
  "task_prompt": "Build the requested feature and commit the result.",
  "task_kind": "feature_branch_pr",
  "task_class": "docs_task",
  "owned_paths": ["README.md", "docs/**"],
  "agent": "codex",
  "model": null,
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

`scripts/run_awf.py` is the compatibility dogfood runner for exercising the same
building blocks outside the always-on service. It stores its SQLite DB under
`--work-dir`, does not require the local Postgres control-plane database,
provisions workspaces, launches Docker Compose, runs the agent, creates a PR,
and runs the PR monitor. The service worker is the normal always-on executor;
use the script for isolated experiments, checked-in specs, release/sync
compatibility runs, and SQLite-backed throwaway runs.

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
  - OpenCode via Ollama auth/state in `~/.config/opencode` and `~/.ollama`.

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
The image includes the Docker CLI and Docker Compose plugin so DinD profiles
can run project Compose diagnostics inside the workspace sidecar. Rebuild this
image whenever the runtime Dockerfile or those Docker tooling packages change.

```bash
docker build -t awf-agent-runtime:latest -f docker/agent-runtime.Dockerfile .
```

Verify:

```bash
docker image inspect awf-agent-runtime:latest
```

### Configure Environment

Local service development should use Postgres via the Compose stack. The
service worker needs a GitHub token for PR creation, review-thread inspection,
and merges; `AWF_GITHUB_TOKEN` is preferred, while `GH_TOKEN` and
`GITHUB_TOKEN` are accepted fallbacks.

```bash
cp .env.example .env
export AWF_GITHUB_TOKEN="$(gh auth token)"
# Optional: mirror Compose-interpolated values into docker/compose/.env.
printf 'AWF_GITHUB_TOKEN=%s\n' "$AWF_GITHUB_TOKEN" > docker/compose/.env
uv run --python 3.12 --extra dev awf service bootstrap
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
OPENAI_API_KEY=<optional Codex env auth>
ANTHROPIC_API_KEY=<optional Claude env auth>
GEMINI_API_KEY=<optional Gemini env auth>
AWF_OPENCODE_OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
AWF_AGENT_WALL_TIMEOUT_SECONDS=7200
AWF_AGENT_IDLE_TIMEOUT_SECONDS=900
AWF_COMPLETED_WORKSPACE_RETENTION_HOURS=168
AWF_WORKSPACE_CLEANUP_ENABLED=true
AWF_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS=3600
AWF_WORKSPACE_CLEANUP_BATCH_LIMIT=50
```

Agent watchdogs are conservative by default: AWF terminates a coding CLI after
7200 seconds of wall-clock runtime or 900 seconds without stdout/stderr output.
Partial stdout/stderr is kept in workspace logs for salvage and diagnosis.

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
- `~/.config/opencode` and small `~/.ollama` auth files copied into
  per-workspace isolated auth directories for OpenCode/Ollama runs.
- selected provider environment variables.

Prefer declaring the credentials a workspace needs in the profile:

```yaml
secrets:
  - name: github-token
    kind: env
    target: GH_TOKEN
    provider: github
    ref: token
  - name: openai-token
    kind: env
    target: OPENAI_API_KEY
    provider: env
    ref: env/OPENAI_API_KEY
  - name: github-cli-config
    kind: mount
    target: /home/agent/.config/gh
    provider: local-auth
    ref: .config/gh
```

Local env leases support `provider: env` with `ref: NAME` or `ref: env/NAME`.
GitHub env leases use the first available `AWF_GITHUB_TOKEN`, `GH_TOKEN`, or
`GITHUB_TOKEN` and expose `GH_TOKEN` plus `GITHUB_TOKEN` placeholders inside the
agent container. Local mount leases support `provider: host-file` /
`provider: local-file` for exact existing host files, and
`provider: local-auth` / `provider: auth` for known read-only auth refs such as
`.config/gh`, `.config/gcloud`, `.gitconfig`, and `.ssh`. AWF records lease
issue/mount/expiry/revoke metadata, provider names, targets, counts, and compose
paths. It does not persist or log secret values, and this local slice does not
broker Vault, AWS, GCP Secret Manager, or other cloud secrets.

Codex auth is intentionally isolated per workspace because a live host
`~/.codex` contains state and locks that can collide with Codex Desktop.
OpenCode/Ollama auth is isolated for the same reason: the agent can refresh
local provider state without mutating the operator's live config.

For local service mode, these host paths must be visible to the worker at their
host absolute paths. `docker/compose/local-service.yml` does this by mounting
only the listed credential paths read-only into the control-plane containers;
the worker copies only Codex `auth.json`, `config.toml`, `installation_id`, and
`rules/`, plus OpenCode config and Ollama auth files, into
`${AWF_HOST_WORK_DIR:-${HOME}/.awf/service}/auth/<workspace>/...` before
launching the workspace stack. AWF does not copy `~/.ollama/models`; workspace
OpenCode runs talk to the host Ollama daemon through `host.docker.internal`.

Profile lint blocks profile-declared service volumes that mount `${HOME}`,
`${AWF_HOST_HOME}`, `~`, `/home/<user>`, or `/Users/<user>` into broad auth
locations such as `/home/agent` or `/root`. Declared local-file lease refs that
point at those broad host-home roots are also rejected. The only
local-development compatibility exception is the credential path list above,
mounted read-only; set `security.host_home_auth_mounts.mode: warn` to allow
those narrow mounts with a structured warning. Writable host-home credential
mounts and writable declared local auth leases are rejected; seed writable auth
into AWF's per-workspace auth directory instead.

Readiness checks use the same service-visible signals without reading secret
file contents:

- GitHub: `AWF_GITHUB_TOKEN`, `GH_TOKEN`, or `GITHUB_TOKEN`, plus a bounded
  `gh auth status` check for PR creation, comments, and merges.
- Codex: isolated per-workspace copies from `~/.codex`, or Codex/OpenAI static
  env auth such as `OPENAI_API_KEY`.
- Claude Code: `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`,
  `CLAUDE_CODE_OAUTH_TOKEN`, `~/.claude`, or `~/.claude.json`.
- Gemini: `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `GOOGLE_CLOUD_ACCESS_TOKEN`,
  visible `GOOGLE_APPLICATION_CREDENTIALS`, or `~/.gemini`.
- OpenCode/Ollama: `~/.config/opencode`, selected small `~/.ollama` auth files,
  `OLLAMA_API_KEY`, and a cheap Ollama `/api/version` reachability probe.
- Docker: configured Docker host/socket control and Docker registry auth signals
  such as `DOCKER_AUTH_CONFIG` or `~/.docker/config.json`. Docker CLI and daemon
  health remain separate readiness resource checks.

The top-level `agent_readiness.security` summary aggregates warning counts,
provider names, and reason codes such as `STATIC_TOKEN_FALLBACK` or
`DOCKER_HOST_BROAD_CONTROL`.

Use strict checks before provider-specific work:

```bash
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf service status --provider claude_code --format pretty
uv run --python 3.12 --extra dev awf service status --provider codex --format pretty
curl 'http://localhost:8000/readyz?provider=opencode'
```

Default agent models and effort are centralized in
`src/awf/adapters/defaults.py`:

| Agent | Default model | AWF effort |
| --- | --- | --- |
| `claude_code` | `claude-opus-4-7` | `xhigh` mapped to Claude Code `max` |
| `codex` | `gpt-5.5` | `xhigh` via `model_reasoning_effort` |
| `gemini` | `gemini-3.1-pro-preview` | `xhigh` mapped to Gemini `HIGH` thinking |
| `opencode` | `ollama/kimi-k2.6:cloud` | `xhigh` maps to OpenCode `--variant max --thinking` plus Ollama `think` |

If a local subscription or provider account cannot use a default model, choose a
supported model in the task or adapter configuration. In the v2 API, set
`task.model` to override the selected agent's default for that workspace.
For example, Gemini dogfood tests can use a Flash preview model when Pro is
unavailable. OpenCode model overrides use the `ollama/<model>` form, for example
`ollama/glm-5.1:cloud`, `ollama/gemma4:31b-cloud`, or
`ollama/deepseek-v4-pro:cloud`.

### Database Migrations

SQLite local API runs create tables automatically at startup. For Postgres, the
preferred bootstrap command runs migrations through the Compose `migrate`
service before starting the API and worker:

```bash
uv run --python 3.12 --extra dev awf service bootstrap
```

Manual Compose migration:

```bash
docker compose -f docker/compose/local-service.yml up --build --force-recreate migrate
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

For DB-backed projects that do not need nested Docker, declare the app and
database as profile services in AWF's outer workspace stack. The agent reaches
them by Compose service name on `awf_net`; no host port is required.

Example repo-local Python/Postgres profile:

```yaml
awf:
  name: my-db-backed-app
  version: 1
  docker:
    mode: none
  runtime:
    environment:
      APP_BASE_URL: http://app:8080
      DATABASE_URL: postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf
  services:
    - name: postgres
      image: postgres:16-alpine
      environment:
        POSTGRES_DB: awf
        POSTGRES_PASSWORD: ${AWF_POSTGRES_PASSWORD}
        POSTGRES_USER: awf
      healthcheck_cmd: pg_isready -U awf -d awf
      volumes:
        - [postgres_data, /var/lib/postgresql/data]
    - name: app
      build_context: .
      dockerfile: Dockerfile
      environment:
        DATABASE_URL: postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf
        PORT: "8080"
      depends_on:
        - postgres
      healthcheck_cmd: python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=5).read()"
      command: python /app/app.py
  phases:
    setup:
      - command: python -c "import os, urllib.request; urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/setup', timeout=10).read()"
        timeout_seconds: 30
    validate:
      - command: python -c "import os, urllib.request; urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/validate', timeout=10).read()"
        timeout_seconds: 30
  validation:
    healthchecks:
      - name: app
        command: python -c "import os, urllib.request; urllib.request.urlopen(os.environ['APP_BASE_URL'] + '/healthz', timeout=10).read()"
        timeout_seconds: 30
  ports:
    app: http://app:8080
    postgres: postgresql://awf:${AWF_POSTGRES_PASSWORD}@postgres:5432/awf
```

This Postgres service is workspace-local project data. It is distinct from the
AWF control-plane Postgres database used by the API and worker.

The same outer-stack service model also covers queue-backed applications. The
generic fixture at `tests/fixtures/workspace_services/redis_worker_app` declares
Redis, an HTTP app, and a worker sidecar with no host port requirements:

```yaml
name: redis-worker-app
version: 1
docker:
  mode: none
runtime:
  environment:
    APP_BASE_URL: http://app:8080
    REDIS_URL: redis://redis:6379/0
    WORKER_STATUS_URL: http://app:8080/status
services:
  - name: redis
    image: redis:7-alpine
    healthcheck_cmd: redis-cli ping
    volumes:
      - [redis_data, /data]
  - name: app
    build_context: .
    environment:
      REDIS_URL: redis://redis:6379/0
      PORT: "8080"
    depends_on:
      - redis
    healthcheck_cmd: python /app/scripts/container_healthcheck.py app
    command: python /app/app.py
  - name: worker
    build_context: .
    environment:
      REDIS_URL: redis://redis:6379/0
      WORKER_ID: redis-worker-fixture
    depends_on:
      - redis
    healthcheck_cmd: python /app/scripts/container_healthcheck.py worker
    command: python /app/worker.py
```

The app, worker, and Redis talk by Compose service name on the per-workspace
network. AWF renders the named Redis volume with a workspace-prefixed Docker
volume name and owns teardown through `docker compose down -v`, so cleanup
removes the app, worker, Redis container, network, and workspace-scoped volume.

Frontend projects use the same profile-service model. A Node or Next-style app
can run as one service, with browser validation isolated in a Playwright sidecar
that the agent triggers over the workspace network. No host port is required,
and this is project validation rather than the AWF console/dashboard.

Example repo-local Node/Next plus browser validation profile:

```yaml
name: my-next-app
version: 1
docker:
  mode: none
runtime:
  environment:
    APP_BASE_URL: http://app:3000
    BROWSER_VALIDATE_URL: http://browser:9323/validate
services:
  - name: app
    build_context: .
    dockerfile: Dockerfile
    environment:
      PORT: "3000"
    healthcheck_cmd: node /app/scripts/container-healthcheck.mjs http://127.0.0.1:3000/healthz ok
    command: npm run start
  - name: browser
    build_context: .
    dockerfile: Dockerfile.playwright
    environment:
      APP_BASE_URL: http://app:3000
      PORT: "9323"
    depends_on:
      - app
    healthcheck_cmd: node /app/scripts/container-healthcheck.mjs http://127.0.0.1:9323/healthz ok
    command: node /app/browser/validator-server.mjs
phases:
  setup:
    - command: node scripts/setup.mjs
      timeout_seconds: 30
  validate:
    - command: node scripts/validate-browser.mjs
      timeout_seconds: 120
validation:
  healthchecks:
    - name: app
      command: node scripts/healthcheck.mjs app
      timeout_seconds: 30
    - name: browser
      command: node scripts/healthcheck.mjs browser
      timeout_seconds: 30
ports:
  app: http://app:3000
  browser: http://browser:9323/validate
```

AWF starts and tears down both services with the per-workspace Compose stack.
The agent container runs setup and validation commands from `/workspace`, while
browser automation stays inside the Playwright service.

## Observability

AWF includes a local Next.js console under `apps/console`. It talks to AWF
through Next.js BFF routes, so `AWF_API_TOKEN` stays server-side and is never
sent to browser JavaScript.

Start the full local service stack, which sets `AWF_API_TOKEN=local-dev-token`
by default:

```bash
uv run --python 3.12 --extra dev awf service bootstrap
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
- `POST /v1/workspaces/{id}/remonitor` for audited operator PR-monitor recovery; CLI: `awf workspace remonitor <id> --idempotency-key <key>`.
- `POST /v1/workspaces/{id}/refresh`, `/validate`, and `/rebase` for async
  operator recovery operations. Duplicate `Idempotency-Key` replays return the
  stored operation after later workspace state changes; fresh-key active
  coalescing still checks current state, so refresh coalesces are rejected once
  destruction starts.
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
