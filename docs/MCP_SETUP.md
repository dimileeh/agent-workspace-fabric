# MCP Setup

AWF exposes a local stdio MCP server for agent orchestrators that support MCP.
Use it when Claude Code or Codex should call AWF through typed tools instead of
shelling out to `awf` or `curl`.

## Prerequisites

Install AWF and start the local Core service first. Persist the required local
service values before bootstrapping so Compose and the MCP server read the same
API, worker, and Postgres service environment:

```bash
uv tool install agent-workspace-fabric
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"
export AWF_GITHUB_TOKEN="$(gh auth token)"
export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"
{
  printf 'AWF_API_TOKEN=%s\n' "$AWF_API_TOKEN"
  printf 'AWF_POSTGRES_PASSWORD=%s\n' "$AWF_POSTGRES_PASSWORD"
  printf 'AWF_POSTGRES_HOST_PORT=%s\n' "$AWF_POSTGRES_HOST_PORT"
  printf 'AWF_DATABASE_URL=%s\n' "$AWF_DATABASE_URL"
  printf 'AWF_GITHUB_TOKEN=%s\n' "$AWF_GITHUB_TOKEN"
} > .env
awf service bootstrap
awf service status --format pretty
```

For contributor checkouts, install from source instead:

```bash
git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
cd aira-agent-workspace-fabric
uv tool install . --force
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"
export AWF_GITHUB_TOKEN="$(gh auth token)"
export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"
mkdir -p docker/compose
{
  printf 'AWF_API_TOKEN=%s\n' "$AWF_API_TOKEN"
  printf 'AWF_POSTGRES_PASSWORD=%s\n' "$AWF_POSTGRES_PASSWORD"
  printf 'AWF_POSTGRES_HOST_PORT=%s\n' "$AWF_POSTGRES_HOST_PORT"
  printf 'AWF_DATABASE_URL=%s\n' "$AWF_DATABASE_URL"
  printf 'AWF_GITHUB_TOKEN=%s\n' "$AWF_GITHUB_TOKEN"
} > docker/compose/.env
awf service bootstrap
awf service status --format pretty
```

`awf service bootstrap` is the current runnable local Core startup path. Source
checkouts use `docker/compose/.env` as the local service environment; package
installs use `.env` near the working directory instead. Pass the env file
explicitly when configuring MCP; `awf mcp serve --env-file` requires the file to
exist so the MCP process sees the same database and token settings as the local
service.

Project onboarding is separate from service startup. After local Core is
running, use `awf init <path>` when you want AWF to create or validate a
repository's `.awf/workspace.yml`.

## Claude Code

Register AWF as a local stdio MCP server:

```bash
claude mcp add --transport stdio --scope local awf -- \
  awf mcp serve --env-file /absolute/path/to/docker/compose/.env
```

For package installs that use a project-local `.env`, point `--env-file` at
that file instead.

## Codex

Add AWF to the Codex MCP server configuration:

```toml
[mcp_servers.awf]
command = "awf"
args = ["mcp", "serve", "--env-file", "/absolute/path/to/docker/compose/.env"]
startup_timeout_sec = 20
tool_timeout_sec = 120
```

Restart the Codex session after editing the configuration.

## Supported Tools

The MCP server exposes the same safe control-plane operations documented in
[MCP Reference](MCP_REFERENCE.md), including workspace creation, PR monitor
adoption, bounded logs/artifacts, metrics, service readiness, and audited
workspace controls.

MCP does not expose arbitrary shell, Docker exec, host filesystem browsing, or
secret material. Agent runtimes currently implemented inside AWF workspaces are
Codex, Claude Code, Gemini, and OpenCode.
