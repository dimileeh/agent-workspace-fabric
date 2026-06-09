# MCP Setup

AWF exposes a local stdio MCP server for agent orchestrators that support MCP.
Use it when Claude Code or Codex should call AWF through typed tools instead of
shelling out to `awf` or `curl`.

## Prerequisites

Install AWF and start the local Core service first. Persist the required local
service values in root `.env` so Compose and the MCP server read the same API,
worker, and Postgres service environment.

For package installs, create that file explicitly because `.env.example` stays
inside the installed AWF package assets:

```bash
uv tool install agent-workspace-fabric
cat > .env <<'EOF'
AWF_API_TOKEN=local-dev-token
AWF_POSTGRES_PASSWORD=awf_dev
AWF_API_HOST_PORT=8000
AWF_POSTGRES_HOST_PORT=5433
AWF_CONSOLE_HOST_PORT=3000
EOF
awf setup
awf start
awf service status --format pretty
```

For contributor checkouts, install from source instead:

```bash
git clone https://github.com/dimileeh/agent-workspace-fabric.git
cd agent-workspace-fabric
uv tool install . --force
cp .env.example .env
awf setup
awf start
awf service status --format pretty
```

`awf start` is the friendly local Core startup path. Source checkouts and
package installs both use root `.env` as the local service environment. Pass
that env file explicitly when configuring MCP; `awf mcp serve --env-file`
requires the file to exist so the MCP process sees the same database and token
settings as the local service.

Project onboarding is separate from service startup. After local Core is
running, use `awf init <path>` when you want AWF to create or validate a
repository's `.awf/workspace.yml`.

## Claude Code

Register AWF as a local stdio MCP server:

```bash
claude mcp add --transport stdio --scope user awf -- \
  awf mcp serve --env-file /absolute/path/to/.env
```

Use an absolute path to the same root `.env` you use to start AWF.

## Codex

Add AWF to the Codex MCP server configuration:

```toml
[mcp_servers.awf]
command = "awf"
args = ["mcp", "serve", "--env-file", "/absolute/path/to/.env"]
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
