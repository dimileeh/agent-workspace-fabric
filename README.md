# Aira Agent Workspace Fabric (AWF)

**Profile-driven, isolated, reproducible execution substrate for AI coding agents.**

AWF is an agent-agnostic service that any orchestrator or coding agent can call
to run one coding task end-to-end in an isolated Docker workspace: check out
source, resolve a workspace profile, launch Codex / Claude Code / Gemini inside,
run profile-declared setup and validation phases, and submit a PR.

It exposes both a **REST API** (for shell-style invocation from OpenClaw skills) and an
**MCP server** (for Codex / Claude Code to invoke AWF as a typed tool).

Status: **v0.1.0 MVP** — parallel-execution primitive only. Stale detection, auto-rebase,
merge queue, and task-class lock matrix are deferred to v0.2 (Phase 1.5).

See [`docs/PLAN_MVP.md`](docs/PLAN_MVP.md) for scope, design decisions, and task breakdown.

## Quick start

```bash
# Install with dev deps
uv venv --python 3.12
source .venv/bin/activate
uv pip install -e ".[dev]"

# Run the test suite
pytest
```

## Architecture

See [`docs/PLAN_MVP.md`](docs/PLAN_MVP.md) for the full architecture overview.

High-level: a single FastAPI process hosts both the REST API and the MCP server,
backed by a Postgres control-plane database. An async worker drives the
workspace lifecycle: git worktree creation → profile resolution → compose stack
provisioning → profile setup → coding agent execution → profile validation → PR
creation → cleanup.

## Workspace profiles

Project-specific behavior lives in workspace profiles, not in the AWF control
plane. AWF resolves profiles in this order:

1. Inline profile in the v2 request.
2. Repo-local `.awf/workspace.yml`.
3. Built-in profile registry (`generic`, `python`, `node`, `nextjs`,
   `docker-compose`, `aira`).
4. Auto-detection from repo files.
5. Low-confidence `generic` fallback.

The base compose stack is now only the agent container plus profile-declared
services. Postgres, pgvector, `AIRA_DATABASE_URL`, Alembic, Redis, app services,
Playwright, and per-workspace DinD are profile data.

Preview profile resolution for a local checkout:

```bash
awf profile preview ~/Projects/example-repo --profile auto
```

Create a v2 workspace:

```bash
awf workspace create \
  --repo git@github.com:example/app.git \
  --base main \
  --profile auto \
  --agent codex \
  --title "Implement feature" \
  --prompt "Build the requested feature and commit the result." \
  --test "pytest -q"
```

## License

Apache-2.0. See [LICENSE](LICENSE).
