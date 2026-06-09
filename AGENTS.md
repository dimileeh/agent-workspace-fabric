# AGENTS.md

This repository is AWF: Agent Workspace Fabric. Treat it as the control plane
for many autonomous engineering agents working on the same repositories in
isolated workspaces, like a very large engineering team with a disciplined PR
integrator.

## Product Vision

AWF should make it safe to run hundreds or thousands of agent engineers in
parallel. Each workspace gets an isolated checkout, runtime, services,
validation, logs, artifacts, PR, and PR monitor. The control plane owns
lifecycle, scheduling, merge safety, validation provenance, stale detection,
comment handling, cleanup, and observability. Agents write code; AWF owns the
workspace and integration process.

The long-term model is:

- many independent workspaces can work on overlapping areas;
- owned paths are coordination and stale-detection hints, not ordinary
  admission blockers;
- explicit exclusive locks are reserved for truly serialized resources such as
  schema lineages, generated artifacts, or future policy-defined critical
  sections;
- merge conflicts and PR review feedback are resolved through the PR monitor
  loop, the way a strong integration engineer would resolve them;
- no PR is merged until comments, checks, freshness, validation provenance, and
  merge policy are clean.

## Read First

- `README.md` explains the current implementation and local service setup.
- `docs/awf_prd_v2.2.md` is the product contract. When behavior is ambiguous,
  prefer the PRD unless the code has a clearly documented newer decision.
- The roadmap and open work live in [GitHub Issues](https://github.com/dimileeh/agent-workspace-fabric/issues).
  Hosted, GKE, and multi-tenant layers remain future work beyond this local Core.
- `.awf/workspace.yml` is this repository's own AWF profile. Keep it generic
  and project-local; do not hard-code project-specific assumptions into AWF core
  (those belong in a profile, e.g. the built-in `aira` compatibility profile).

## Engineering Rules

- Use strict TDD for behavior changes: write or update the failing test first,
  confirm the failure when practical, implement, then make the smallest green
  change.
- Add regression tests for every bug fix. A bug without a regression test is
  considered unfinished unless the reason is documented.
- Preserve or improve coverage. This repo declares a 99% coverage target in
  `.awf/workspace.yml`; if a task cannot meet that target yet, leave the code
  measurably closer and explain the gap in the PR.
- Keep changes scoped. Avoid broad refactors unless they are required by the
  requested behavior or the PRD slice being implemented.
- Prefer existing patterns in `src/awf` over new abstractions.
- Do not hide failures behind retries. Retries must preserve reason codes,
  logs, and events.

## Plan-and-Validate Workflow (Mandatory)
For non-trivial work, always follow `plans/PLAN_EXECUTION_PROTOCOL.md`.

Required:
- Save implementation plan to `plans/<TOPIC>_PLAN.md` before coding.
- Execute work against that saved plan.
- Validate implementation against the original plan in `plans/<TOPIC>_VALIDATION.md`.
- If gaps are found, run another iteration focused on the highest-impact gap and update the validation doc

## Validation Commands

Use the narrowest commands that prove the change, then run the broader surface
when the touched area justifies it.

Python/control-plane:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
```

OpenAPI spec drift check (fails if generated spec diverges from checked-in openapi.json):

```bash
python scripts/generate_openapi.py --check
```

Console:

```bash
npm --prefix apps/console run lint
npm --prefix apps/console run typecheck
npm --prefix apps/console run build
```

Coverage, when the task touches core behavior broadly:

```bash
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing
```

## AWF Dogfood Rules

- For substantial AWF work, prefer launching an AWF workspace rather than doing
  everything by hand.
- Use `auto_merge=true` by default when the target branch is not `main`.
- Do not manually resolve comments, push fix commits, or merge PRs that AWF is
  supposed to monitor, unless the human explicitly asks for manual intervention.
- PR monitors must wait for review grace, read unresolved actionable comments,
  address them, push fixes, and keep monitoring until merge or terminal failure.
- Non-actionable bot noise should not trigger human escalation, but it also
  must not bypass review-grace timing.

## Architecture Boundaries

- AWF core is generic: profiles carry project-specific runtime, services,
  validation, secrets, ports, monitor policy, and coverage expectations.
- Aira-specific database, pgvector, Alembic, and service assumptions belong only
  in an Aira profile.
- The control plane owns Docker lifecycle. Agents may use Docker for diagnosis
  and tests, but they are not the lifecycle authority.
- Workspaces are the operator-facing unit. Task, attempt, canonical attempt,
  validation run, stale reason, and merge candidate records are the merge-safety
  backbone beneath that surface.
- Local Postgres is the AWF control-plane database. Project databases remain
  isolated per workspace/profile.

## Safety

- Never log tokens or secrets.
- Prefer declared secret mounts or leases over ad hoc host-home access as that
  support matures. Note: Profile-level security, egress policy, and secret declarations define the contract locally; the full cloud secret broker is a future addition.
- Do not use destructive git operations against user work. If the worktree is
  dirty, inspect before editing and preserve unrelated changes.
- Cleanup must remove workspace containers, networks, volumes, pressure
  directories, and worktrees only when policy says they are no longer needed.
