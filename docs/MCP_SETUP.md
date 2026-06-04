# MCP Setup

AWF exposes a local stdio MCP server for agent orchestrators that support MCP.
Use it when Claude Code or Codex should call AWF through typed tools instead of
shelling out to `awf` or `curl`.

## Prerequisites

Install AWF and start the local Core service first. Persist the required local
service values before setup/start so Compose and the MCP server read the same
API, worker, and Postgres service environment:

```bash
uv tool install agent-workspace-fabric
export AWF_API_TOKEN="$(openssl rand -hex 32)"
export AWF_POSTGRES_PASSWORD="${AWF_POSTGRES_PASSWORD:-awf_dev}"
export AWF_POSTGRES_HOST_PORT="${AWF_POSTGRES_HOST_PORT:-5433}"
export AWF_GITHUB_TOKEN="$(gh auth token)"
export AWF_DATABASE_URL="postgresql+asyncpg://awf:${AWF_POSTGRES_PASSWORD}@localhost:${AWF_POSTGRES_HOST_PORT}/awf"
awf_env_tmp="$(mktemp)"
{
  printf 'AWF_API_TOKEN=%s\n' "$AWF_API_TOKEN"
  printf 'AWF_POSTGRES_PASSWORD=%s\n' "$AWF_POSTGRES_PASSWORD"
  printf 'AWF_POSTGRES_HOST_PORT=%s\n' "$AWF_POSTGRES_HOST_PORT"
  printf 'AWF_DATABASE_URL=%s\n' "$AWF_DATABASE_URL"
  printf 'AWF_GITHUB_TOKEN=%s\n' "$AWF_GITHUB_TOKEN"
  if [ -f .env ]; then
    sed \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_API_TOKEN[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_PASSWORD[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_POSTGRES_HOST_PORT[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_DATABASE_URL[[:space:]]*=/d' \
      -e '/^[[:space:]]*\(export[[:space:]][[:space:]]*\)\{0,1\}AWF_GITHUB_TOKEN[[:space:]]*=/d' \
      .env
  fi
} > "$awf_env_tmp"
mv "$awf_env_tmp" .env
awf setup
awf start
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
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
```

`awf setup` checks the host and selected environment; `awf start` starts local
Core. Source checkouts use `docker/compose/.env` as the local service
environment; pass `--source-checkout "$PWD"` from the checkout you just cloned
so setup/start refresh and use that checkout even if older source-checkout
metadata exists. Package installs use `.env` near the working directory instead.
Pass the env file explicitly when configuring MCP; `awf mcp serve --env-file`
requires the file to exist so the MCP process sees the same database and token
settings as the local service.

Project onboarding is separate from service startup. After local Core is
running, use `awf init <path>` when you want AWF to create or validate a
repository's `.awf/workspace.yml`.

## Claude Code

Register AWF as a local stdio MCP server:

```bash
claude mcp add --transport stdio --scope user awf -- \
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

## Assisted client setup

Instead of editing the client config by hand, `awf setup --client claude` and
`awf setup --client codex` register the AWF MCP server for you. The assisted
path prefers the official client CLI (`claude` / `codex`) when it is on `PATH`
and otherwise edits the structured config file directly. It:

- prints a diff of the change before writing,
- writes a timestamped backup of any existing config file before replacing it,
- refuses ambiguous conflicts (an existing `awf` server entry that points
  somewhere else) instead of overwriting them,
- supports `--dry-run` to preview the diff without mutating anything, and
- never reads, accepts, or stores provider tokens — it only records the
  `--env-file` path the server should read.

```bash
awf setup --client claude --dry-run   # preview the change
awf setup --client claude             # apply it (writes a backup first)
```

## Supported Tools

The MCP server exposes the same safe control-plane operations documented in
[MCP Reference](MCP_REFERENCE.md), including workspace creation, PR monitor
adoption, bounded logs/artifacts, metrics, service readiness, and audited
workspace controls.

MCP does not expose arbitrary shell, Docker exec, host filesystem browsing, or
secret material. Agent runtimes currently implemented inside AWF workspaces are
Codex, Claude Code, Cursor, Gemini, and OpenCode.
