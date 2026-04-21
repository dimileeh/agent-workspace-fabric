# Aira Agent Workspace Fabric (AWF)

**Isolated, reproducible execution substrate for AI coding agents.**

AWF is an agent-agnostic service that any coding agent (OpenClaw, Codex, Claude Code) can call
to run one coding task end-to-end in an isolated Docker workspace: check out source, launch the
coding agent inside, run tests (with sidecar services like Postgres + Alembic migrations as the
repo profile requires), and submit a PR.

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

High-level: a single FastAPI process hosts both the REST API and the MCP server, backed by a
Postgres control-plane database. An async worker (using `SELECT FOR UPDATE SKIP LOCKED`, no Redis
or Celery) drives the workspace lifecycle: git worktree creation → compose stack provisioning
→ coding agent execution → validation → PR creation → cleanup.

## License

Apache-2.0. See [LICENSE](LICENSE).
