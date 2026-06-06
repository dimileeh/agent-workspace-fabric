# Getting Started

> **Note:** For a quick, step-by-step introduction, see the [Quickstart](QUICKSTART.md).

## Prerequisites

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
  - Cursor CLI auth through `CURSOR_API_KEY`.
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

### Installation

AWF currently has three runnable first-run lanes:

- `uv tool` / `pipx` for a release-installed package.
- Source checkout with a global tool install for inspectable source plus an
  `awf` executable on `PATH`.
- Source checkout with no global install for inspectable source run through
  `uv run`.

The public curl installer lane is release-gated until the hosted installer URL,
manifest, checksums, and release artifacts are published and verified.

The recommended primary path is to install AWF as an isolated CLI tool via `uv tool`:

```bash
uv tool install agent-workspace-fabric
```

`pipx` provides the same isolated CLI-tool model:

```bash
pipx install agent-workspace-fabric
```

If you prefer to install it into an existing virtualenv, use:

```bash
python -m venv .venv
. .venv/bin/activate
pip install agent-workspace-fabric
```

For contributors who want to modify AWF itself:

```bash
git clone git@github.com:dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install . --force
uv sync
```

Homebrew is planned after the Python package has stable tagged artifacts and a
passing formula audit; it is not a supported install channel yet.

### Recommended First-Run Sequence

Once AWF is installed, the recommended first-run sequence is setup, start,
health check, then project onboarding. Keep local runtime values in the install
root `.env` so the CLI, worker, MCP server, and raw Docker Compose lane all read
the same configuration:

```bash
mkdir -p "$HOME/awf-eval-project"
cp .env.example .env
awf setup
awf start
awf service status --format pretty
awf init "$HOME/awf-eval-project"
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

`awf setup` checks host readiness, imports any legacy `docker/compose/.env`
values into root `.env`, and configures supported clients such as MCP when
requested. `awf start` starts the local AWF Core stack, including the web
console at <http://127.0.0.1:3000>, and
`awf service status --format pretty` confirms API, database, Docker, image,
disk, provider, and cleanup health. The lower-level
`awf service bootstrap` command remains available for service-only development
and is what `awf start` delegates to.

Use `awf start --headless` when a host should not run the console, or
`awf start --console-port 3333` when port `3000` is already in use.

For the source checkout with global tool install lane, run from the checkout:

```bash
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf init "$HOME/awf-eval-project"
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

For the source checkout with no global install lane, run from the checkout:

```bash
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf init "$HOME/awf-eval-project"
uv run --python 3.12 --extra dev awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

For source checkouts or raw Docker installs, root Compose can bring up the full
local stack with safe loopback-only defaults:

```bash
docker compose up -d --build
```

Open <http://127.0.0.1:3000> for the console, or call the API at
<http://127.0.0.1:8000>. Protected local API calls use
`Authorization: Bearer local-dev-token` unless you set `AWF_API_TOKEN`.

If setup, startup, or first-run health checks fail, use the
[First run troubleshooting guide](TROUBLESHOOTING.md#first-run-troubleshooting)
before continuing with provider or workspace-level work.

After local Core reports success, use `awf init <path>` to create or inspect a
project repository's `.awf/workspace.yml` (see
[Project Onboarding](PROJECT_ONBOARDING.md) for the project-mode walkthrough and
per-provider copy-paste prompts).

- `awf init <path>` — run local project onboarding. Interactive terminals get
  a short guided profile setup; automation can use
  `awf init <path> --write-profile --yes` to write detected defaults.

Subsequent sections describe the contributor/development setup; a fresh
machine only needs the steps above plus a coding-agent credential.

If the PR already exists and you only need AWF to monitor it, use the supported
adoption path instead of rerunning the original coding agent. See
[PR Monitor Adoption](PR_MONITOR_ADOPTION.md) for CLI, REST, MCP, GitHub auth,
monitor policy, idempotency, console inspection, and mocked-local validation.

### Configure Environment

Root `.env` is the single local runtime env file for source checkouts and
package installs when you want to override local defaults. `awf setup`,
`awf start`, `awf service bootstrap`, and raw root `docker compose` all use that
file. Existing legacy
`docker/compose/.env` files are treated only as a migration source; setup/start
bootstrap imports missing keys into root `.env`, backs up the legacy file, and
reports only key names.

Local service development should use Postgres via the Compose stack. The
service worker needs a GitHub token for PR creation, review-thread inspection,
and merges; `AWF_GITHUB_TOKEN` is preferred, while `GH_TOKEN` and
`GITHUB_TOKEN` are accepted fallbacks.

```bash
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_GITHUB_TOKEN="$(gh auth token)"
export AWF_POSTGRES_HOST_PORT=${AWF_POSTGRES_HOST_PORT:-5433}
export AWF_API_HOST_PORT=${AWF_API_HOST_PORT:-8000}
{
  grep -vE '^(AWF_API_TOKEN|AWF_GITHUB_TOKEN)=' .env.example
  printf 'AWF_API_TOKEN=%s\n' "$AWF_API_TOKEN"
  printf 'AWF_GITHUB_TOKEN=%s\n' "$AWF_GITHUB_TOKEN"
  printf 'AWF_POSTGRES_HOST_PORT=%s\n' "$AWF_POSTGRES_HOST_PORT"
  printf 'AWF_API_HOST_PORT=%s\n' "$AWF_API_HOST_PORT"
} > .env
uv run --python 3.12 --extra dev awf service bootstrap
```

For API-only throwaway development, use the local PostgreSQL control-plane DB:

```bash
export AWF_DATABASE_URL="postgresql+asyncpg://awf:awf_dev@localhost:5433/awf"
export AWF_API_TOKEN="$(openssl rand -hex 32)"
uv run --python 3.12 --extra dev awf serve --host 127.0.0.1 --port 8000
```

Key local service values:

```text
AWF_POSTGRES_HOST_PORT=5433
AWF_API_HOST_PORT=8000
AWF_DATABASE_URL=postgresql+asyncpg://awf:awf_dev@localhost:5433/awf
AWF_API_TOKEN=<local bearer token>
AWF_AGENT_RUNTIME_IMAGE=awf-agent-runtime:latest
AWF_HOST_WORK_DIR=${HOME}/.awf/service
AWF_HOST_HOME=${HOME}
AWF_HOST_SSH_AUTH_SOCK=<optional Linux SSH_AUTH_SOCK override>
AWF_GITHUB_TOKEN=<token from gh auth token>
OPENAI_API_KEY=<optional Codex env auth>
ANTHROPIC_API_KEY=<optional Claude env auth>
CURSOR_API_KEY=<optional Cursor env auth>
GEMINI_API_KEY=<optional Gemini env auth>
AWF_OPENCODE_OLLAMA_BASE_URL=http://host.docker.internal:11434/v1
COMPOSE_PROFILES=ollama-bridge
AWF_OLLAMA_BRIDGE_BIND_ADDRESS=172.17.0.1
AWF_AGENT_WALL_TIMEOUT_SECONDS=7200
AWF_AGENT_IDLE_TIMEOUT_SECONDS=3600
AWF_COMPLETED_WORKSPACE_RETENTION_HOURS=168
AWF_WORKSPACE_CLEANUP_ENABLED=true
AWF_WORKSPACE_CLEANUP_SCAN_INTERVAL_SECONDS=3600
AWF_WORKSPACE_CLEANUP_BATCH_LIMIT=50
AWF_NETWORK_POSTURE_OPEN_LEGACY_CUTOFF=<optional ISO-8601 rollout instant>
```

If you change `AWF_POSTGRES_HOST_PORT` and set `AWF_DATABASE_URL` to a
non-default value, update its localhost port to match.

For host-side `awf` workspace commands and manual HTTP checks, `AWF_BASE_URL`
is the operator-facing API knob. Usually you do not need to set it: when
`AWF_API_HOST_PORT` is present, the CLI derives `http://localhost:<port>`
automatically. Set `AWF_BASE_URL` only when running host-side CLI or HTTP checks
from a different shell that does not carry `AWF_API_HOST_PORT`, or when
targeting a reverse proxy or other non-derived API root. `AWF_CLI_BASE_URL`
still works for compatibility, but is deprecated.

```bash
export AWF_API_HOST_PORT=9001
export AWF_BASE_URL="http://localhost:${AWF_API_HOST_PORT}"
awf workspace list --format pretty
curl "${AWF_BASE_URL}/readyz?provider=github"
```

`AWF_API_BASE_URL` is different: it is the API self-reference URL used by
service-side doctor, smoke, and status checks. Local Compose sets that in the
service container to `http://api:8000`; do not use it as the host CLI target.

### Local vs Production Configuration

The bundled defaults are for `AWF_ENV=local` and `AWF_ENV=ci`. In those modes,
the local Compose database URL and callback defaults remain usable for
development and tests. Set `AWF_API_TOKEN` to a local bearer token before
starting service containers or protected API controls.

For a network-facing deployment, set `AWF_ENV=prod`. AWF validates production
settings during service configuration and API startup, then fails fast with
structured diagnostics before opening the database or admitting work if local
development defaults are still active.

Production must set:

- `AWF_DATABASE_URL` to a production PostgreSQL database with
  deployment-specific credentials. Do not use the bundled local
  `awf` / `awf_dev` credentials.
- `AWF_API_TOKEN` to a deployment-specific high-entropy bearer token. Missing
  values, short values, and local placeholders such as `local-dev-token`,
  `changeme`, or `default` are rejected.
- `AWF_CALLBACKS_ENABLED=false` unless callback delivery is intentionally used.
  For production callback delivery, keep the strong API token, set
  `AWF_CALLBACKS_REQUIRE_HTTPS=true`, and set
  `AWF_CALLBACKS_ALLOWED_HOSTS` to the exact callback hosts you operate.

Production validation diagnostics name the unsafe setting and remediation, but
they do not print raw tokens, database passwords, or full secret-bearing
database URLs.

Agent watchdogs are conservative by default: AWF terminates a coding CLI after
7200 seconds of wall-clock runtime or 900 seconds without stdout/stderr output.
Partial stdout/stderr is kept in workspace logs for salvage and diagnosis.

The local service uses the configured PostgreSQL control-plane DB. Set
`AWF_DATABASE_URL` before bootstrapping the service when you need an isolated
control plane.

### Agent Credentials in Containers

Local service worker-created workspace stacks map local auth into the agent
container:

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
only the listed credential paths read-only into the API and worker containers;
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
- Cursor: `CURSOR_API_KEY`; no host credential directory mount is required.
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
uv run --python 3.12 --extra dev awf service status --provider cursor --format pretty
curl 'http://localhost:8000/readyz?provider=opencode'
```

Default agent models and effort are centralized in
`src/awf/adapters/defaults.py`:

| Agent | Default model | AWF effort |
| --- | --- | --- |
| `claude_code` | `claude-opus-4-8` | `xhigh` passed through to Claude Code |
| `codex` | `gpt-5.5` | `xhigh` via `model_reasoning_effort` |
| `cursor` | `sonnet-4-thinking` | `xhigh` uses the thinking-capable model variant; no separate Cursor effort flag |
| `gemini` | `gemini-3.1-pro-preview` | `xhigh` mapped to Gemini `HIGH` thinking |
| `opencode` | `ollama/kimi-k2.6:cloud` | `xhigh` maps to OpenCode `--variant max --thinking` plus Ollama `think` |

If a local subscription or provider account cannot use a default model, choose a
supported model in the task or adapter configuration. In the workspace create
request, set `task.model` to override the selected agent's default for that
workspace.
For example, Gemini dogfood tests can use a Flash preview model when Pro is
unavailable. OpenCode model overrides use the `ollama/<model>` form, for example
`ollama/glm-5.1:cloud`, `ollama/gemma4:31b-cloud`, or
`ollama/deepseek-v4-pro:cloud`.

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
5. Use `awf workspace create` to submit the task to the local service.
6. Watch the workspace through `awf workspace show`, `awf workspace logs`, or
   the console.
7. Expect lifecycle evidence such as:
   - `agent.run.start`
   - `agent.run.ok`
   - `pr.created`
   - `monitor.action`
   - `monitor.initial_review_grace_waiting`
   - `monitor.compose_teardown_ok`

## Local Dogfood Runner

The local service worker is the normal always-on executor. Submit dogfood work
through the API, CLI, MCP, or console; all four surfaces create the same
control-plane workspace rows in the PostgreSQL control-plane DB.

Example CLI submission:

```bash
uv run --python 3.12 --extra dev awf workspace create \
  --repo git@github.com:dimileeh/aira-agent-workspace-fabric.git \
  --base development \
  --profile auto \
  --agent codex \
  --title "Add workspace list filters for operator console" \
  --prompt "Implement the requested feature with tests." \
  --test "uv run --python 3.12 --extra dev pytest tests/unit -q" \
  --auto-merge \
  --format json
```

Use `awf workspace show <workspace_id> --format pretty`,
`awf workspace logs <workspace_id>`, or the console to follow progress. Use
`awf service gc` for service-owned cleanup rather than deleting workspaces by
hand.

For cross-repo E2E tasks, pass managed companions through REST, MCP, or CLI
workspace creation. For example, a web repo can request a backend companion
with `--companion-json '{"name":"backend","repo_url":"git@github.com:example/api.git","base_branch":"development"}'`.
AWF clones the companion into a service-owned worktree and tears it down with
the parent workspace; companion paths are repo-relative and never raw host
path passthroughs.
