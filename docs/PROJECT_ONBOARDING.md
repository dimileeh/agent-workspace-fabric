# AWF Project Onboarding

This page is the copy-paste contract for making an existing repository usable
with AWF without launching a workspace. The first pass should inspect the repo,
draft or write `.awf/workspace.yml`, show gaps, and stop. Humans or agents can
then edit the draft and decide when to submit a real workspace request.

## First-run operator command

Use the first-run commands in order:

- `awf setup` — prepare this machine for AWF.
- `awf start` — start or validate local AWF Core.
- `awf init <path>` — the project-onboarding pass described on this page. It
  inspects a checked-out repository, runs local readiness checks without
  calling the AWF API, and creates or previews `.awf/workspace.yml`. Interactive
  terminals get a short guided setup when no profile exists; automation can use
  `--write-profile --yes` to write detected defaults.

```bash
awf setup
awf start
awf init .                        # guided project onboarding for ./.
awf init . --write-profile --yes  # silent profile write with detected defaults
awf init . --include-smoke-request
```

## One-message prompt

Use this with Codex, Claude Code, Gemini, OpenCode, OpenClaw, or a human
operator:

```text
Inspect this repository for AWF onboarding. Do not launch a workspace, push,
open a PR, or start project services. Start with:

`awf init . --write-profile --yes`
`awf profile preview .`

or use `awf init . --include-smoke-request` for a local copy-paste smoke request
payload (local-only, no submission). Keep secrets as
profile declarations or `${VAR}` placeholders; never write raw secret values.
```

## Per-provider copy-paste prompts

Each prompt below is tuned for the provider's CLI or chat style. They are
generic for any repository and do not contain project-specific assumptions.

### Codex

```text
Inspect this repository for AWF onboarding. Do not launch a workspace yet.

1. Run `awf init . --write-profile --yes` to inspect the repo and write `.awf/workspace.yml`.
2. Run `awf profile preview .` to preview the resolved profile.
3. Run `awf init . --include-smoke-request` to produce a smoke workspace request, then submit it to validate the profile end-to-end.
4. Once the smoke workspace succeeds, implement the requested feature through AWF by creating a real workspace with an appropriate task prompt and owned paths.

Keep secrets as profile declarations or `${VAR}` placeholders; never write raw secret values.
```

### Claude Code

```text
I will inspect this repository for AWF onboarding. I will not launch a workspace yet.

1. Run `awf init . --write-profile --yes` to inspect the repo and write `.awf/workspace.yml`.
2. Run `awf profile preview .` to preview the resolved profile.
3. Run `awf init . --include-smoke-request` to produce a smoke workspace request, then submit it to validate the profile end-to-end.
4. Once the smoke workspace succeeds, implement the requested feature through AWF by creating a real workspace with an appropriate task prompt and owned paths.

Keep secrets as profile declarations or `${VAR}` placeholders; never write raw secret values.
```

### Gemini

```text
Analyze this repository for AWF onboarding. Do not launch a workspace yet.

1. Run `awf init . --write-profile --yes` to inspect the repo and write `.awf/workspace.yml`.
2. Run `awf profile preview .` to preview the resolved profile.
3. Run `awf init . --include-smoke-request` to produce a smoke workspace request, then submit it to validate the profile end-to-end.
4. Once the smoke workspace succeeds, implement the requested feature through AWF by creating a real workspace with an appropriate task prompt and owned paths.

Keep secrets as profile declarations or `${VAR}` placeholders; never write raw secret values.
```

### OpenCode

```text
Use `opencode run` to inspect this repository for AWF onboarding. Do not launch a workspace yet.

1. Run `awf init . --write-profile --yes` to inspect the repo and write `.awf/workspace.yml`.
2. Run `awf profile preview .` to preview the resolved profile.
3. Run `awf init . --include-smoke-request` to produce a smoke workspace request, then submit it to validate the profile end-to-end.
4. Once the smoke workspace succeeds, implement the requested feature through AWF by creating a real workspace with an appropriate task prompt and owned paths.

Keep secrets as profile declarations or `${VAR}` placeholders; never write raw secret values.
```

### OpenClaw

```text
Inspect this repository for AWF onboarding via your OpenClaw runtime. Do not launch a workspace yet.

1. Run `awf init . --write-profile --yes` to inspect the repo and write `.awf/workspace.yml`.
2. Run `awf profile preview .` to preview the resolved profile.
3. Run `awf init . --include-smoke-request` to produce a smoke workspace request, then submit it to validate the profile end-to-end.
4. Once the smoke workspace succeeds, implement the requested feature through AWF by creating a real workspace with an appropriate task prompt and owned paths.

Keep secrets as profile declarations or `${VAR}` placeholders; never write raw secret values.
```

## CLI flow

Preview only:

```bash
awf profile preview .
```

Write a draft profile:

```bash
awf init . --write-profile --yes
```

Overwrite an existing draft intentionally:

```bash
awf init . --write-profile --yes --force
```

Include an example request body without launching it:

```bash
awf init . --include-smoke-request
```

`awf profile init` remains available as a lower-level preview/write command for
scripts and expert workflows. Both onboarding commands are local filesystem
work: they do not call the AWF API, create a workspace, start Docker, push
branches, or open a PR.

Generated onboarding profiles use `security.egress.mode: restricted` by
default. Keep that default for new projects unless the repository needs
explicitly trusted local dogfood behavior. Choosing `open` is allowed, but it is
intentional unrestricted internet access and should be visible in review.

## Templates

`awf init <path>` and `awf profile init` currently draft these small templates:

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

`--include-smoke-request` prints a workspace request-shaped payload with the
draft profile inlined:

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

## DX smoke proof (post-onboarding)

After completing onboarding, validate the full AWF surface with a single command:

```bash
awf smoke run --format pretty
```

See [docs/SMOKE_COMMAND.md](SMOKE_COMMAND.md) for the complete phase reference,
reason codes, and mocked-local mode details.

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
