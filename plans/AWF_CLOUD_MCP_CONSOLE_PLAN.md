# AWF Cloud MCP Console Plan

Status: in progress
Date: 2026-05-18
Owner: Codex

## Problem Statement

Create a detailed execution plan for the first hosted AWF product slice based on
`docs/AWF_ON_GKE_PRD.md`. The plan must cover:

- A backend service that owns registration, login, account management,
  organization/project policy, OAuth, API keys, and remote MCP access.
- A hosted remote MCP service for AWF that users can add to Codex or Claude by
  OAuth or API key.
- A hosted Next.js + Tailwind CSS console at `awf.aira.pro` for registration,
  login, account management, and workspace monitoring.
- An MVP that is credible now but does not paint AWF into a corner before the
  GKE industrial-scale architecture is built.

## Scope

In scope:

- Produce `docs/AWF_CLOUD_MCP_CONSOLE_EXECUTION_PLAN.md`.
- Anchor the plan in existing AWF Core code paths where possible.
- Include backend architecture, data model, auth model, remote MCP protocol
  shape, console routes, deployment shape, milestones, acceptance criteria,
  and test strategy.
- Validate the plan against the user request and the GKE PRD.

Out of scope:

- Implement backend or console code in this turn.
- Pick a final paid identity vendor contract.
- Build the full GKE cell scheduler, fleet controller, billing engine, or
  enterprise SSO package in this MVP plan.

## Requirements Checklist

- [ ] Backend service owns human auth boundary and product account state.
- [ ] Backend service supports registration, login, account management,
      organizations, projects, members, repo bindings, API keys, and audit.
- [ ] Remote MCP endpoint is specified for Codex and Claude.
- [ ] OAuth path follows current MCP authorization expectations for HTTP
      transports.
- [ ] API key path is available for automation and clients that cannot complete
      OAuth.
- [ ] Console is Next.js with Tailwind CSS and includes registration, login,
      account management, MCP setup, and workspace monitoring.
- [ ] Plan explains how existing AWF Core REST/MCP/console code is reused.
- [ ] Plan preserves the private cloud repo boundary from the GKE PRD.
- [ ] Plan has phased milestones with implementation tasks and pass criteria.
- [ ] Plan includes test coverage, security, performance, observability, and
      rollout gates.

## Implementation Steps

1. Review `docs/AWF_ON_GKE_PRD.md`, existing FastAPI auth, existing FastMCP
   server, and existing Next console proxy/dashboard.
2. Verify current public protocol expectations for remote MCP, OAuth, Codex,
   Claude, Next.js auth, and Tailwind setup from official documentation.
3. Draft the execution plan in `docs/AWF_CLOUD_MCP_CONSOLE_EXECUTION_PLAN.md`.
4. Create `plans/AWF_CLOUD_MCP_CONSOLE_VALIDATION.md` with requirement-by-
   requirement status and evidence.
5. Run lightweight validation commands that prove the new Markdown files exist
   and contain the required sections.

## Verification Commands

```bash
test -f docs/AWF_CLOUD_MCP_CONSOLE_EXECUTION_PLAN.md
test -f plans/AWF_CLOUD_MCP_CONSOLE_PLAN.md
test -f plans/AWF_CLOUD_MCP_CONSOLE_VALIDATION.md
rg -n "OAuth|API key|Next.js|Tailwind|remote MCP|awf.aira.pro|Acceptance" docs/AWF_CLOUD_MCP_CONSOLE_EXECUTION_PLAN.md
```

Pass criteria:

- All files exist.
- The execution plan contains backend, MCP, console, deployment, test,
  security, scale, and acceptance sections.
- The validation file marks all user-request requirements complete or records
  explicit defer rationale.
