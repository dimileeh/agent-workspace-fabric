# Aira Agent Workspace Fabric (AWF)

*New to AWF? See the [Start Here Quickstart](docs/START_HERE.md) to bootstrap a local evaluation workspace in under 5 minutes.*

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
- [docs/AWF_CORE_TRUST_MODEL.md](docs/AWF_CORE_TRUST_MODEL.md) for the local
  Core trust boundary and future Operator/Architect split.
- [docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md](docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md)
  for the local control-plane container UID/GID strategy and per-pillar
  analysis behind the root-by-default decision.

## Documentation

- [Getting Started](docs/GETTING_STARTED.md) / [Start Here Quickstart](docs/QUICKSTART.md)
- [Concepts & Architecture](docs/CONCEPTS.md)
- [CLI Reference](docs/CLI_REFERENCE.md)
- [REST API Reference](docs/REST_API_REFERENCE.md)
- [MCP Reference](docs/MCP_REFERENCE.md)
- [Client Surfaces](docs/CLIENT_SURFACES.md)
- [Troubleshooting](docs/TROUBLESHOOTING.md)
- [Trust Model](docs/AWF_CORE_TRUST_MODEL.md)
- [Contributor Guide](CONTRIBUTING.md)

## Supported Client Surfaces (v0.1)

REST, CLI, and MCP are the supported client surfaces for v0.1. AWF does not currently ship with a supported Python SDK. Integrators should use one of the supported surfaces (e.g., the CLI for operator convenience or the REST API for control-plane programmatic access). Please do not import internal AWF modules (such as `awf.*` or other internal paths) to build custom API clients, as they are not part of the stable public contract and are subject to change without notice.

## PR Monitor Adoption

Existing GitHub pull requests can be adopted into AWF monitoring through the
REST, CLI, and MCP surfaces. Adoption creates a monitor-owned workspace for the
open PR without re-running the coding agent, then lets AWF apply the normal PR
monitor loop for comments, checks, freshness, and merge policy.

## License

Apache-2.0. See [LICENSE](LICENSE).
