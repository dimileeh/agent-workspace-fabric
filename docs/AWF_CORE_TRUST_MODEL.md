# AWF Core Trust Model

AWF Core is a deterministic local workspace fabric. It creates isolated
checkouts, runtime containers, service stacks, validation runs, logs, artifacts,
pull requests, PR monitor loops, and cleanup decisions. It does not contain an
LLM planner inside the controller. AI operators, architects, and product
planners should sit above Core and use AWF through the REST, CLI, and MCP
surfaces.

```text
AWF Architect / Operator / Aira planner
        |
        v
REST / CLI / MCP
        |
        v
AWF Core: lifecycle, policy, validation, PR monitor, cleanup
```

## Local Boundary

AWF Core runs with the local permissions granted to the service process and the
Docker daemon. On a developer machine, Docker access is powerful: a workspace
container may be isolated from other workspaces, but the daemon itself can start
containers, attach networks, mount declared paths, and consume local CPU, disk,
and memory. Treat Docker daemon access as a privileged local trust boundary.

The local control plane (`api`, `worker`, `migrate`) runs as `root` inside its
container by design so it can use the host Docker socket, the Docker Desktop
SSH-agent forwarder, and chown per-workspace state to the unprivileged `agent`
user (UID/GID `1000`) that the agent runtime container runs as. Workspace
state under `AWF_HOST_WORK_DIR` is therefore root-owned on the host on Linux
and is normally cleaned up through `awf service gc` rather than host `rm`. See
[docs/AWF_LOCAL_CONTAINER_UID_STRATEGY.md](AWF_LOCAL_CONTAINER_UID_STRATEGY.md)
for the per-pillar analysis (Docker socket, SSH/auth mounts, bind-mounted AWF
state, linked worktree metadata, Linux/macOS behavior, cleanup permissions,
migration path) and the locked test contract.

AWF enforces workspace-level lifecycle, declared profile configuration,
validation provenance, PR monitor policy, stale detection, and cleanup. It does
not make an untrusted local machine safe, and it does not replace OS-level
sandboxing, endpoint security, or secret hygiene.

## Internet Egress

Profiles define local egress posture. Generated onboarding profiles default to
restricted egress so new projects start conservatively. A project may choose
open egress, as this repository currently does for dogfooding multiple LLM and
package-provider paths.

Open egress is useful for local developer experimentation, package installs,
provider CLIs, documentation lookup, and real-world dogfooding. It also means
agent-written commands can contact the public internet unless blocked by the
host, network, profile, or future policy layers. Use restricted egress for
client repositories, sensitive codebases, or reproductions where deterministic
network access matters.

## Secrets And Credentials

AWF Core should prefer declared profile secrets, explicit environment mounts,
and future lease-based secret brokers over ad hoc host-home access. Local Core
does not guarantee that a tool running inside a workspace cannot misuse a
secret that was intentionally mounted into that workspace.

Provider credentials such as Codex, Claude Code, Cursor, Gemini,
OpenCode/Ollama, and GitHub tokens are operational credentials. They should be
scoped, revocable, and kept out of logs. AWF’s diagnostics and status surfaces
should redact known secret values, but operators should still avoid placing
secrets in prompts, task titles, repository files, or review comments.

## Untrusted Text

Agents read issue descriptions, PR comments, code review comments, dependency
metadata, docs, websites, package output, and logs. Treat all of that text as
untrusted input. AWF Core should preserve raw evidence for auditability while
keeping policy decisions deterministic. It should not let external text bypass
validation, freshness, review-grace, merge policy, or cleanup rules.

The future AWF Operator or Architect layer may reason semantically about
architecture, overlap, backlog priority, and merge risk. That layer should be
considered advisory unless Core has deterministic policy hooks for the decision.

## Package Installs

Workspace setup commands may install packages from public registries or project
mirrors. AWF Core records validation and command provenance, but it does not
fully solve package supply-chain risk locally. Projects that need tighter
control should pin dependencies, use lockfiles, route package access through
approved registries, and keep egress restricted.

## What AWF Core Enforces Locally

- Workspace lifecycle state and terminal-state guards.
- Isolated worktrees and profile-declared runtime/services.
- Validation commands, health checks, freshness, and provenance.
- PR creation and PR monitor loops with review-grace and actionable-comment
  handling.
- Failure taxonomy, retry lineage, provider recovery state, and circuit-breaker
  evidence.
- Orphan, stranded resource, and terminal workspace cleanup policy.
- Operator-facing status, doctor, metrics, logs, and release-readiness checks.

## What AWF Core Does Not Enforce Locally

- A complete OS sandbox around the Docker daemon.
- Semantic architecture review across unrelated workspaces.
- Guaranteed safe execution of arbitrary package install scripts.
- Guaranteed safe use of credentials intentionally mounted into a workspace.
- Cloud-grade secret leasing, network policy, or tenant isolation.
- Human-level product judgment about whether a task should exist.

Those are either host/platform responsibilities or future AWF Operator,
Architect, and cloud-control-plane layers above deterministic Core.
