# AWF Concepts

## Architecture

```text
        Human / planner / MCP client / local script
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
        | codex / claude / cursor / gemini   |
        | opencode / grok                    |
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

Profiles keep AWF generic rather than tied to one application stack.

A `WorkspaceProfile` can describe:

- `runtime`: agent image, toolchain image, environment variables, and declared
  language toolchains (e.g. the JDK versions the build needs — see
  [Declaring language toolchains](#declaring-language-toolchains)).
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

1. Inline profile in the workspace create request.
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
      - command: uv run --python 3.12 --extra dev ruff check src/awf/cli tests/unit/cli
        timeout_seconds: 300
      - command: uv run --python 3.12 --extra dev mypy src/awf/cli
        timeout_seconds: 300
      - command: uv run --python 3.12 --extra dev pytest tests/unit/cli -q
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

This works the same way for Codex, Claude Code, Cursor, Gemini, OpenCode, Grok,
and future adapters because the control plane invokes normal non-interactive agent
runs for each phase and stores the plan/report inside the workspace.

Preview the profile AWF would resolve for a checkout:

```bash
uv run --python 3.12 --extra dev awf profile preview . --profile auto
```

### Declaring language toolchains

`runtime.toolchains` lets a profile declare the language toolchain versions the
build needs, so satisfying them is a preflight/runtime concern rather than an
ad-hoc agent repair. It maps a language identifier to the versions that must be
available in the runtime/toolchain image:

```yaml
# .awf/workspace.yml — a Gradle repo that builds with JDK 17 but also wants 21 on hand
gradle-service:
  name: gradle-service
  version: 1
  description: Gradle service that runs its test suite on JDK 17.
  runtime:
    toolchains:
      java: ["17", "21"]
  phases:
    setup:
      - command: ./gradlew --no-daemon dependencies
    validate:
      - command: ./gradlew --no-daemon test
```

The declaration is optional and backward-compatible: an absent `toolchains` map
(the default) means no toolchain requirement and changes nothing. Language keys
are normalized to lowercase and must be safe identifiers (`^[a-z][a-z0-9+_.-]*$`);
each version is a dotted-numeric string (`17`, `21`, `1.8`, `11.0.2`), de-duplicated
while preserving order. Declarations are bounded (at most 16 languages, 16 versions
each).

The runtime/toolchain image is expected to provide each declared version side by
side — for example JDKs installed under `/usr/lib/jvm/temurin-17` and
`/usr/lib/jvm/temurin-21`, selected per command via `JAVA_HOME` or
`update-alternatives`. When a declared version is not present in the image, the
pure, I/O-free `runtime_toolchain_findings` lint seam yields a
`RUNTIME_TOOLCHAIN_UNAVAILABLE` warning (instead of leaving the agent to install a
JDK by hand). A preflight that introspects the runtime/toolchain image must supply
the seam the versions it discovered; that image introspection is not wired into
`awf profile doctor` yet, so the declaration is advisory until then. The built-in
`java` profile declares `java: ["17", "21"]` for exactly this reason: a real Gradle
repo needed JDK 17 for test execution while the runtime shipped JDK 21.

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

### Local service port defaults and portability

AWF’s local service compose stack keeps container ports stable (`postgres:5432` and
`api:8000`) while allowing host-port overrides for machine portability:

- `AWF_POSTGRES_HOST_PORT` controls the host-facing Postgres port (default `5433`).
- `AWF_API_HOST_PORT` controls the host-facing API port (default `8000`).
- `AWF_BASE_URL` optionally overrides the host/operator API root used by `awf`
  workspace commands and manual HTTP checks.
- `AWF_API_BASE_URL` is the service-side API self-reference URL. In local
  Compose containers it is `http://api:8000`, not the host CLI target.

Postgres stays loopback-bound (`127.0.0.1`) by default. The API preserves Docker's
legacy default host bind behavior for `8000:8000`, so existing local clients that
reach the host IP continue to work. If `AWF_API_HOST_PORT` changes, client calls
that target the local API host must use the matching host URL. The host CLI
derives `http://localhost:<port>` automatically when `AWF_API_HOST_PORT` is set
in the same shell. Set `AWF_BASE_URL` only when the CLI or manual HTTP checks
run from a shell that does not carry `AWF_API_HOST_PORT`, when targeting a
reverse proxy, or when using another non-derived API base URL. `AWF_CLI_BASE_URL`
still works for compatibility, but is deprecated.

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

For operator visibility, `monitor.action` logs now report:

- `review_feedback`: total outside-diff/issue review items fetched in `unresolved_review_comments`.
- `pending_review_feedback`: items in that set still requiring agent triage after monitor-state/body-hash/verdict checks.
- `blocking_reviews`: count of effective GitHub blockers in `status.blocking_reviews`.
- `unresolved_reviews`: maintained as a backward-compatible alias of `review_feedback` for
  existing log consumers.

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

### Awaiting-Required-Checks Grace

When a required CI context is expected but absent on the current head — the
common case right after the monitor pushes fix commits, when the forge has not
started CI on the new head yet — AWF applies a bounded `awaiting_required_checks`
grace window before escalating `NotifyHuman`:

```yaml
monitor:
  awaiting_required_checks_grace_seconds: 600
```

Rules:

- Default is 600 seconds (covers the observed ≈5.5-min CI-start lag with margin).
- `0` (or any value `<= 0`) disables the grace: a required context that is
  absent escalates `NotifyHuman` immediately (pre-#655 behavior).
- The grace is head-scoped: a new `head_sha` starts a fresh window.
- During the window the monitor stays in `WaitForCI` instead of escalating; if
  CI genuinely never arrives, the window expires and the head escalates as
  before.
- The upper bound is 86400 seconds (parity with the other monitor knobs).

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

The control-plane containers run as `root` by design, and the agent runtime
container runs as the unprivileged `agent` user (UID/GID `1000`). Per-workspace
worktrees and the agent-writable subset of the bare-mirror admin metadata are
chowned to UID/GID `1000` after `git worktree add` so the agent can run
`git status`, `git add`, and `git commit` inside `/workspace`. Because the
control plane is `root`, the host directory at `AWF_HOST_WORK_DIR` is normally
root-owned on Linux. Use `awf service gc` to clean up workspace state from the
in-container worker rather than `rm` from the host shell. See
[AWF_LOCAL_CONTAINER_UID_STRATEGY.md](AWF_LOCAL_CONTAINER_UID_STRATEGY.md)
for the full per-pillar analysis (Docker socket, SSH/auth mounts, bind-mounted
state, linked worktree metadata, Linux/macOS behavior, cleanup permissions,
migration path) and the locked test contract.

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
mount the whole home directory. It also forwards the operator's SSH-agent socket
to `/run/host-services/ssh-auth.sock` inside the control-plane containers so
service-worker Git operations can use the operator's loaded SSH keys. On Linux,
the host source falls back to `$SSH_AUTH_SOCK`; set `AWF_HOST_SSH_AUTH_SOCK` if
the shell running Docker Compose does not expose it. Set `AWF_HOST_HOME` if that
shell does not expose the operator home as `${HOME}`.

Credential values used by Compose interpolation must be present in the shell
that starts the stack or in the root `.env`. The same root `.env` is also read
by Python `awf` commands. On macOS,
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
docker compose up -d --build
```

That raw Compose path builds the control-plane image, builds
`awf-agent-runtime:latest`, starts Postgres, runs migrations, starts the API and
worker, and serves the local console at <http://127.0.0.1:3000>. It uses
loopback-only local defaults for `AWF_API_TOKEN` and `AWF_POSTGRES_PASSWORD`;
set explicit values in root `.env` when you want a non-default local token,
database password, provider credential, or port.

On Linux, host Ollama is often bound only to `127.0.0.1:11434`, which Docker
containers cannot reach through `host.docker.internal`. AWF includes a
Linux-only optional `ollama-bridge` Compose profile that binds a host-network
socat listener on the Docker bridge address and forwards it to host-local
Ollama. It is disabled by default and is not needed on macOS Docker Desktop.

```bash
cat >> .env <<'EOF'
COMPOSE_PROFILES=ollama-bridge
AWF_OPENCODE_OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
OLLAMA_HOST=http://host.docker.internal:11434
AWF_OLLAMA_BRIDGE_BIND_ADDRESS=172.17.0.1
AWF_OLLAMA_BRIDGE_LISTEN_PORT=11434
AWF_OLLAMA_BRIDGE_TARGET_HOST=127.0.0.1
AWF_OLLAMA_BRIDGE_TARGET_PORT=11434
EOF
uv run --python 3.12 --extra dev awf service bootstrap --provider opencode
```

If your Docker host gateway is not `172.17.0.1`, set
`AWF_OLLAMA_BRIDGE_BIND_ADDRESS` to the address that
`host.docker.internal:host-gateway` resolves to for local containers.

Inspect the service and logs from another terminal:

```bash
uv run --python 3.12 --extra dev awf service status
uv run --python 3.12 --extra dev awf service status --provider github --format pretty
uv run --python 3.12 --extra dev awf service readiness --format json
uv run --python 3.12 --extra dev awf service release-readiness --format pretty
curl 'http://localhost:8000/readyz?provider=github'
curl 'http://localhost:8000/release-readiness'
uv run --python 3.12 --extra dev awf service logs --follow --service worker
```

If `AWF_API_HOST_PORT` is customized, host CLI calls derive the matching
localhost URL automatically when the variable is present in the same shell. Set
`AWF_BASE_URL` for manual HTTP diagnostics or when CLI checks run from a shell
that does not carry the host-port override:

```bash
export AWF_BASE_URL="http://localhost:${AWF_API_HOST_PORT}"
curl "${AWF_BASE_URL}/readyz?provider=github"
curl "${AWF_BASE_URL}/release-readiness"
```

`awf service status` and `/readyz` include an `agent_readiness` section for
GitHub, Codex, Claude Code, Cursor, Gemini, OpenCode/Ollama, Grok, and Docker. Each
provider reports redacted `credential_sources`, `credential_scope`,
`isolation`, and structured warnings. Missing optional providers and local
least-privilege downgrades are warnings by default. Pass `--provider <name>` or
`?provider=<name>` to make that provider strict for scheduling or rollout
checks.

`awf service readiness --format json` is the executable local Core release
scorecard; `awf service release-readiness` is the same gate under a clearer
name. It aggregates service readiness, doctor diagnostics with cached status
reuse, provider readiness, cleanup/orphan posture, PRD SLO thresholds, recent
failure taxonomy, and the maintained `examples/awf-core-demo` onboarding smoke
evidence. The same report is exposed through `GET /release-readiness` and the
MCP `awf_get_core_release_readiness` tool. The gate fails when recent
workspace failures still have generic or unknown reason codes, or rolling PRD
SLO metrics are unavailable/stale/below threshold, unless an operator
explicitly runs it with an allowlist flag and records the rationale in the
release ledger.

The demo project also includes an offline executable smoke:

```bash
uv run --python 3.12 --extra dev python examples/awf-core-demo/scripts/core_release_smoke.py
```

It proves the local profile preview and workspace request path, then emits
explicit mocked-local PR monitor and cleanup evidence so the Core demo remains
deterministic without live GitHub or provider credentials.

The service-mode default database URL is local Postgres
(`postgresql+asyncpg://awf:...@localhost:5433/awf`). Tests, throwaway script
runs, and the always-on service all use PostgreSQL.

The local Compose stack defaults `AWF_API_TOKEN` to `local-dev-token` for
loopback-bound source-checkout cold starts. Set a local bearer token in the
shell or `.env` before starting the stack when you want a non-default token; use
the same value for manual console development.

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
docker compose build
docker tag awf-control-plane:local "awf-control-plane:${AWF_LOCAL_VERSION}"
docker tag awf-agent-runtime:latest "awf-agent-runtime:${AWF_LOCAL_VERSION}"
docker image inspect "awf-control-plane:${AWF_LOCAL_VERSION}"
docker image inspect "awf-agent-runtime:${AWF_LOCAL_VERSION}"
```

Run raw Compose commands from the AWF install/source root so Compose reads the
same `.env` file as `awf start` and `awf service bootstrap`. If the needed
Compose variables are already exported in your shell, they still override
matching `.env` entries:

```bash
docker compose build
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
docker compose up -d postgres
docker compose exec -T postgres \
  pg_dump -U awf -d awf -Fc \
  > "$AWF_HOST_WORK_DIR/backups/awf-control-plane-pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).dump"

docker compose build
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
docker compose up -d postgres
docker compose exec -T postgres \
  pg_dump -U awf -d awf -Fc \
  > "$AWF_HOST_WORK_DIR/backups/awf-control-plane-$(date -u +%Y%m%dT%H%M%SZ).dump"
```

Running from the AWF root avoids depending on shell state because Compose reads
root `.env`. If the needed Compose variables are already exported, they override
matching `.env` entries. The Postgres exec prefix is
`docker compose exec -T postgres`, and the pre-restore stop command is
`docker compose stop api worker`.

Restore only when the API and worker are stopped. This avoids live writes
during restore and makes the backup the single source of control-plane truth.
Before restore, stop API and worker.

```bash
export AWF_BACKUP="$HOME/.awf/service/backups/awf-control-plane-YYYYmmddTHHMMSSZ.dump"
docker compose stop api worker
docker compose up -d postgres
docker compose exec -T postgres \
  dropdb -U awf --maintenance-db=postgres --if-exists awf
docker compose exec -T postgres \
  createdb -U awf --maintenance-db=postgres awf
docker compose exec -T postgres \
  pg_restore -U awf -d awf --no-owner < "$AWF_BACKUP"
docker compose up --build --force-recreate migrate
docker compose up -d api worker
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
docker compose up -d --force-recreate api worker
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
[Control-Plane Postgres Backup And Restore](#control-plane-postgres-backup-and-restore) and restart through
`awf service bootstrap`.

### Local Disaster Recovery

For stuck Compose containers, first collect state and logs, then remove only
containers and networks. The default cleanup command below intentionally does
not remove the Postgres volume:

```bash
docker compose ps
uv run --python 3.12 --extra dev awf service logs --tail 200
docker compose stop api worker migrate
docker compose down --remove-orphans
uv run --python 3.12 --extra dev awf service bootstrap
uv run --python 3.12 --extra dev awf service status --format pretty
```

Run cleanup from the AWF root so Compose reads root `.env`; exported variables
override matching `.env` entries. The container/network cleanup command is
`docker compose down --remove-orphans`.

Use `down --volumes` only as a last resort after a verified control-plane
backup exists. Removing the Compose volume destroys the local AWF
control-plane database.

For a corrupt `${AWF_HOST_WORK_DIR}`, quarantine the directory and rebuild a
clean one. Preserve logs, artifacts, backups, and auth when they are still
readable:

```bash
export AWF_HOST_WORK_DIR="${AWF_HOST_WORK_DIR:-$HOME/.awf/service}"
export AWF_QUARANTINE="${AWF_HOST_WORK_DIR}.quarantine.$(date -u +%Y%m%dT%H%M%SZ)"
docker compose stop api worker
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
destructive cleanup, then use the restore flow in [Control-Plane Postgres Backup And Restore](#control-plane-postgres-backup-and-restore).

## PR Monitor Recovery

The monitor is supposed to stay alive until the PR merges, closes, or fails. If
the host restarts, Docker dies, or a worker process is restarted, use the
control-plane adoption and remonitor surfaces instead of legacy helper scripts:

```bash
uv run --python 3.12 --extra dev awf workspace adopt-pr \
  --repo dimileeh/agent-workspace-fabric \
  --pr 123 \
  --auto-merge \
  --format pretty
```

Existing monitor workspaces can also be recovered with
`awf workspace remonitor`, the matching REST control route, or
`awf_remonitor_workspace` through MCP.

### Operator guidance (`guide`/`instruct`)

`remonitor` recovers a *lost* monitor; to **steer a live one**, use the
purpose-named `guide` control (alias `instruct`). It injects an operator
**directive** — a first-class agent instruction, distinct from the audit
`reason` — into a workspace that is still `monitoring_pr`. The monitor's next
`decide()` cycle reads it as a non-deferrable, pending operator hint and
re-engages the agent ("address this, do not defer"), so a workspace parked on a
`NotifyHuman`/human-wait deferral is resumed **without** the destructive
cancel + re-`adopt-pr` dance. Guidance stays advisory: it is context for the
agent's next cycle, never a direct PR mutation.

```bash
uv run --python 3.12 --extra dev awf workspace guide <id> \
  --directive "implement the forge-neutral fix, do not defer" \
  --reason "operator decision recorded" \
  --idempotency-key <key>
```

It mirrors the other controls 1:1 across CLI (`awf workspace guide`/`instruct`),
REST (`POST /v1/workspaces/{id}/guide`), and MCP (`awf_guide_workspace`), and is
idempotent + audited (reason code `OPERATOR_GUIDE`). Historically
`remonitor --reason` doubled as the directive channel (the reason string was fed
to the agent); that still works, but `guide --directive` is the intended,
purpose-named affordance — `remonitor` stays focused on monitor recovery.

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

### Cross-Repo Companion Services

Profile services are still the preferred model for services that live in the
same repository as the workspace. When a task needs a live stack from another
repository, use workspace companions instead. A companion is a managed
repo-relative checkout that AWF materializes beside the primary workspace and
renders into the same Compose project.

Example REST/MCP companion request:

```json
{
  "name": "backend",
  "repo_url": "git@github.com:example/api.git",
  "base_branch": "development",
  "build_context": ".",
  "dockerfile": "Dockerfile",
  "env_file": "config/dev.env",
  "environment": {"APP_ENV": "test"},
  "environment_secrets": {
    "AIRA_API_KEY": {
      "provider": "env",
      "kind": "env",
      "value_from": "ANTHROPIC_API_KEY"
    }
  },
  "compose_up_timeout_seconds": 900,
  "depends_on": ["docker"],
  "healthcheck_cmd": "curl -fsS http://localhost:8000/health",
  "ports": [[8000, 18000]],
  "command": "python -m api",
  "volumes": [["./fixtures", "/fixtures"]]
}
```

`base_branch` defaults to the primary workspace base branch. `build_context`,
`dockerfile`, `env_file`, and relative volume sources are resolved inside the
managed companion checkout; absolute host paths and `..` escapes are rejected.
Companion service names cannot collide with profile services or reserved
services such as `agent` and `docker`.

Companion `environment` values are literal and reject Docker Compose
interpolation such as `$VAR` or `${VAR}`. To pass a host-managed env secret to a
companion, use `environment_secrets`; AWF stores only the source env var name,
then renders an AWF-generated Compose placeholder during stack launch.

By default, `docker.startup_timeout_seconds` from the resolved workspace profile
controls the stack `docker compose up --wait-timeout` value. A companion can
raise that stack-level startup budget with `compose_up_timeout_seconds` when its
Dockerfile needs extra cold-cache build time; AWF uses the maximum companion
override and adds a small subprocess capture buffer.

Companions share the parent workspace resource reservation and lifecycle. AWF
keeps companion worktrees while the parent workspace is active, removes them
with normal destroy/GC cleanup, and classifies
`<workspace_id>__companion__<name>` worktrees as belonging to the parent during
orphan-resource scans.

Companion images are cached across workspaces. Companion services build on the
shared host Docker daemon (the control plane runs `docker compose` against the
host socket), so at provision time AWF pre-builds each companion image once per
`(name, commit sha)` and tags it `awf-companion-<name>:<short_sha>`. Subsequent
workspaces — including a concurrent dispatch wave for the same companion commit —
reference the existing tag via `image:` and skip the build entirely. An
in-process per-tag lock collapses a concurrent wave to a single build; a build
failure falls back to an inline `build:` so provisioning stays correct. The
pre-build is budgeted with the same effective `compose_up_timeout_seconds`
subprocess cap the inline `docker compose up` build uses, so raising that knob for
slow cold-cache builds raises both the cached and inline build allowances together
and the pre-build never times out earlier than the inline build it would replace.
Cached images carry an `awf.managed-companion=true` label, and `awf service gc` prunes
ones older than `companion_image_retention_hours` (Docker never removes an
image backing a live container, so active workspaces are protected). The window is
keyed on image *creation* time, not last use — Docker exposes no last-used filter for
`image prune` — so a still-current image built before the window can be evicted and
rebuilt cold on the next dispatch (a slower launch, not a correctness issue). If you
see a companion rebuild sooner than the retention window implies, it is creation-age
eviction of a stopped-but-not-destroyed workspace's image, not a bug. Set
`companion_image_cache_enabled=false` to disable caching and always build inline.

## Observability

AWF includes a local Next.js console under `apps/console`. It talks to AWF
through Next.js BFF routes, so `AWF_API_TOKEN` stays server-side and is never
sent to browser JavaScript.

Start the full local service and console stack with root Compose:

```bash
docker compose up -d --build
```

Open <http://127.0.0.1:3000>. Protected API calls use
`Authorization: Bearer local-dev-token` unless you set `AWF_API_TOKEN`.

For API-only throwaway development, start the AWF API with a local token:

```bash
AWF_API_TOKEN="$(openssl rand -hex 32)" uv run --python 3.12 --extra dev awf serve --reload
```

For manual console development against an already-running AWF API:

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
- `POST /v1/workspaces/{id}/guide` for an audited operator **directive** into a live monitoring workspace (closes the `NotifyHuman` loop); CLI: `awf workspace guide <id> --directive "..." --idempotency-key <key>` (alias `instruct`); MCP: `awf_guide_workspace`.
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

## Glossary

| Term | Meaning |
| --- | --- |
| AWF | Agent Workspace Fabric. |
| Workspace | One isolated task execution environment and its persisted control-plane row. |
| Profile | Project-specific runtime, services, phases, validation, secrets, and monitor policy. |
| Agent runtime | The coding CLI launched inside the workspace container. |
| PR monitor | Per-workspace loop that owns a PR through comments, CI, base sync, and merge. |
| `AddressComments` | PR monitor action that asks the agent to fix meaningful review feedback. |
| `NotifyHuman` | PR monitor action for manual-merge mode or non-code policy blockers. |
| Initial review grace | One-time wait after PR monitoring starts before auto-merge may happen. |
| DinD | Docker-in-Docker sidecar used for Dockerized projects. |
