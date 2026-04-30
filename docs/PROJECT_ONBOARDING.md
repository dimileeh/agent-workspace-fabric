# AWF Project Onboarding

This page is the copy-paste contract for making an existing repository usable
with AWF without launching a workspace. The first pass should inspect the repo,
draft `.awf/workspace.yml`, show gaps, and stop. Humans or agents can then edit
the draft and decide when to submit a real workspace request.

## First-run operator command

Run `awf init <path>` for the first onboarding pass. It runs local readiness
checks (without calling AWF API), prints plain-language next steps, and points to
profile-edit and smoke-workspace workflows.

```bash
awf init .
awf init . --include-smoke-request
```

## One-message prompt

Use this with Codex, Claude Code, Gemini, OpenCode, OpenClaw, or a human
operator:

```text
Inspect this repository for AWF onboarding. Do not launch a workspace, push,
open a PR, or start project services. Run `awf profile init . --format pretty`
to get a reviewable draft via `awf profile preview .`, then review the diagnostics and run
`awf profile init . --write` only if the draft is useful. Keep secrets as
profile declarations or `${VAR}` placeholders; never write raw secret values.
```

## CLI flow

Preview only:

```bash
awf profile preview .
```

Write a draft profile:

```bash
awf profile init . --write
```

Overwrite an existing draft intentionally:

```bash
awf profile init . --write --force
```

Include an example request body without launching it:

```bash
awf profile init . --include-smoke-request
```

The command is local filesystem work. It does not call the AWF API, create a
workspace, start Docker, push branches, or open a PR.

## Templates

`awf profile init` currently drafts these small templates:

- `generic`: fallback profile when no known project shape is detected.
- `python`: `pyproject.toml`, `requirements.txt`, `uv.lock`, or `pytest.ini`;
  adds Python setup and `pytest -q` validation.
- `node-nextjs`: `package.json` plus `next` dependency or config; uses the
  detected package manager and available lint, test, and build scripts.
- `docker-compose`: one Compose service file; enables DinD and Compose
  setup/cleanup phases.
- `python-postgres`: Python plus `alembic.ini`, Postgres dependencies, or
  `DATABASE_URL`; drafts a Postgres sidecar, secret declaration, database URL,
  and health check.
- `node-playwright`: Node plus `@playwright/test` or `playwright.config.*`;
  adds Playwright validation and reports missing browser/app runtime details.
- `multi-service`: multiple Compose services or common service directories;
  enables DinD when a Compose file is present and reports per-service gaps.

Use `--template <name>` to force a template when the detector is too
conservative.

## Preview diagnostics

The preview always includes separate missing-section lists:

- `missing_services`: expected services that need an explicit profile entry or
  Compose plan, such as browser automation or undeclared service directories.
- `missing_secrets`: secret-like `${VAR}` placeholders seen in Compose that do
  not yet have a profile secret declaration.
- `missing_ports`: services or app endpoints that AWF cannot expose or name.
- `missing_validation_commands`: no lint, test, or build command was inferred.
- `missing_healthchecks`: services or app endpoints without readiness checks.

Diagnostics are review prompts, not hard failures. A draft profile is meant to
round-trip through AWF's `WorkspaceProfile` schema while still being honest
about unknowns.

## Optional smoke request

`--include-smoke-request` prints a v2 request-shaped payload with the draft
profile inlined:

```json
{
  "repo": {"url": "file:///path/to/repo", "base_branch": "main"},
  "task": {
    "title": "Smoke AWF project onboarding profile",
    "prompt": "Run a no-op smoke pass that validates the drafted AWF project profile.",
    "agent": "codex",
    "kind": "feature_branch_pr",
    "auto_merge": false
  },
  "workspace": {"profile_ref": null, "profile": {"name": "generic"}},
  "validation": {"commands": [], "requested_tier": 1},
  "resources": {}
}
```

This is only a shape for review or later copy-paste. The init command never
submits it.

## Secrets

Generated profiles must not contain raw secrets. Use environment placeholders
such as `${POSTGRES_PASSWORD}` and declare expected secrets under `secrets`
with `kind: env` or `kind: mount`. Prefer declared local leases over host-home
service volumes:

```yaml
secrets:
  - name: github-token
    kind: env
    target: GH_TOKEN
    provider: github
    ref: token
  - name: provider-token
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

Local-mode refs currently support `provider: env` with `ref: NAME` or
`ref: env/NAME`, `provider: github` backed by `AWF_GITHUB_TOKEN`, `GH_TOKEN`, or
`GITHUB_TOKEN`, exact existing files via `provider: host-file` /
`provider: local-file`, and known read-only auth refs via
`provider: local-auth` / `provider: auth`. Do not point local-file refs at
`${HOME}`, `${AWF_HOST_HOME}`, `~`, `/home/<user>`, or `/Users/<user>` roots.
Do not paste token values into `.awf/workspace.yml`. AWF records sanitized lease
metadata only; this local path does not implement a cloud secret broker.
