# AWF Full Installer And First-Run Setup

## Summary

Build AWF's first-run experience around four clear jobs:

```text
install AWF  ->  awf setup  ->  awf start  ->  awf init <repo>
```

This is a product-level reset, not a small CLI alias change. AWF should feel
like a modern agent CLI: one trusted installer, one machine setup wizard, one
local Core start command, and one repo onboarding command.

Landscape inputs: Codex emphasizes zero-setup CLI install, Claude Code and Grok
Build lead with native `curl | bash` installers, Hermes uses a one-line
installer plus `pipx`, while `uv tool install` and `pipx install` remain the
clean Python CLI install model.

## Engineering Review Lock

Review date: 2026-05-27.

This plan is the right product direction, but it should not land as one large
implementation PR. The implementation should be split into staged PR slices so
the CLI behavior change, credential storage, MCP integration, and installer
distribution can each be proven with focused tests.

Recommended sequence:

1. **CLI and local setup foundation**
   - Add the `awf setup` and `awf start` command surfaces.
   - Hard-switch no-path `awf init` to a migration error.
   - Add the host setup config model and system-check/readiness payloads.
   - Reuse existing local service bootstrap/status/doctor internals.

2. **Credentials and provider setup**
   - Add keychain-first credential storage and provider refs.
   - Treat GitHub as a first-class provider alongside OpenAI/Codex,
     Claude/Anthropic, Ollama/OpenCode, Gemini, and AWF Cloud.
   - Keep provider auth non-blocking: one failed provider marks only that
     provider unavailable.

3. **Client integration and MCP**
   - Add Claude Code and Codex MCP config writers through `awf setup`.
   - Add MCP tools for setup status, local service start, project init, and
     client integration instructions.
   - Keep raw credential capture in the CLI only; MCP never reads, accepts, or
     returns provider tokens.

4. **Installer and release distribution**
   - Add the checked-in installer script, release manifest generation,
     checksum verification, and install/uninstall tests.
   - Publish release artifacts before advertising the `curl | bash` URL as the
     primary quickstart.

This keeps the full vision intact while avoiding a single diff that touches
installer shell, Python CLI, credential storage, MCP, docs, and release
automation at once.

## What Already Exists

- `awf service bootstrap` already starts local Postgres, migrations, API,
  worker, builds the agent runtime image, and reports structured bootstrap
  failures. `awf start` should be a thin user-facing wrapper over this path,
  not a new launcher.
- `awf service status` and `awf service doctor` already collect Docker, API,
  worker, provider, disk, capacity, and support-bundle diagnostics. `awf setup`
  should reuse those collectors and add missing first-run checks only where
  needed.
- `src/awf/service/provider_readiness.py` already knows GitHub, Codex, Claude
  Code, Gemini, OpenCode/Ollama, Docker, token redaction, and bounded provider
  probes. Setup should convert credential capture into refs consumed by these
  existing readiness checks.
- `awf init <path>` and `awf profile init` already implement project profile
  onboarding. The plan should move no-path machine bootstrap out of `awf init`,
  not rebuild project onboarding.
- `docs/MCP_SETUP.md` already documents Claude Code and Codex stdio setup for
  `awf mcp serve`. `awf setup --client ...` should automate those documented
  shapes with diff/backup/confirmation.
- `.github/workflows/publish.yml` and `RELEASING.md` already build Python
  distributions and checksum artifacts. The installer slice should extend that
  pipeline with an install manifest and installer smoke, not create a parallel
  release process.

Prior learning applied: `awf_near_threshold_backlog` says several AWF modules
and tests are near the 1500-line maintainability threshold. New setup code
should live in new focused modules instead of growing `src/awf/cli/init_ops.py`,
`src/awf/service/provider_readiness.py`, or the existing large CLI/MCP test
parts.

## Proposed Module Boundaries

Use new modules to keep the diff explicit and testable:

```text
src/awf/cli/setup_commands.py
  awf setup CLI wrapper

src/awf/cli/start_commands.py
  awf start CLI wrapper around service bootstrap

src/awf/host_setup/
  config.py              ~/.awf/config.yml schema and IO
  system_checks.py       Docker, Compose, Git, gh, ports, disk, shell/PATH checks
  credentials.py         keyring/env/plain-file credential ref backends
  providers.py           provider setup/readiness orchestration
  clients.py             Claude/Codex MCP config diff, backup, and write helpers
  rendering.py           pretty/json output helpers

src/awf/mcp/setup_tools.py
  awf_get_setup_status
  awf_start_local_service
  awf_initialize_project_profile
  awf_get_client_integration_instructions

packaging/install.sh
  inspected shell entrypoint served as https://aira.pro/awf/install.sh

scripts/generate_install_manifest.py
  release manifest with version, channel, artifact URLs, sha256, and generated_at
```

The existing `src/awf/cli/main.py` should only register the new Typer command
modules. Existing large modules should be touched for delegation and migration
only.

## Credential Architecture

Credential values are never stored in `~/.awf/config.yml`. The config stores
refs and metadata only:

```yaml
providers:
  github:
    credential_ref: keyring://awf/github/token
    source: gh
    status: ready
  openai:
    credential_ref: keyring://awf/openai/api-key
    source: codex-oauth-or-api-key
    status: ready
```

Backends:

- `keyring`: default when a usable system backend exists. This covers macOS
  Keychain and Linux Secret Service/KWallet through the Python `keyring`
  package, with fake backends in tests.
- `env_ref`: stores a variable name such as `OPENAI_API_KEY` or `GH_TOKEN`,
  never the value. This is the safe non-interactive and CI-friendly path.
- `plain_file`: stores an encrypted-at-rest equivalent only when available in a
  future slice; for this plan, raw `chmod 600` plain files are allowed only
  behind `--allow-plain-secrets` plus explicit interactive confirmation or
  non-interactive flag.

`awf setup --non-interactive` must not prompt for secrets. If no keychain/env
ref is provided, it exits with `INTERACTIVE_INPUT_REQUIRED` and a JSON payload
that automation can act on.

MCP tools may report credential status and missing refs, but they must never
take token strings as inputs. Provider login/capture remains a terminal CLI
workflow.

## Installer Architecture

The `curl | bash` path is acceptable only with a pinned, auditable artifact
chain:

```text
install.sh
  |
  +--> resolve channel/version
  +--> download awf-install-manifest.json
  +--> download wheel/sdist artifact from manifest
  +--> verify sha256 before install
  +--> uv tool install /verified/artifact.whl
  +--> verify awf executable, package version, and PATH reachability
```

Preferred install method:

- Download a manifest-pinned wheel from the GitHub release or Aira release
  mirror.
- Verify `sha256` from the manifest before executing any installed code.
- Install the verified local wheel with `uv tool install`.

Fallback install method:

- `uv tool install agent-workspace-fabric==<version>` or `pipx install
  agent-workspace-fabric==<version>` from PyPI.
- This path must stay pinned by version and clearly label that index TLS and
  package-manager integrity are being trusted instead of a pre-downloaded wheel
  checksum.

The installer script must support `--dry-run` without network mutation beyond
manifest fetch, `--version`, `--channel`, `--method`, `--install-dir`,
`--uninstall`, and `--help`. Uninstall removes only files that AWF created and
must refuse to delete an executable it does not recognize as AWF-managed.

## Scope Challenge Outcome

The full plan touches more than eight files and introduces more than two new
services/classes, so the scope is intentionally split. The minimal complete
user-facing path is:

```text
uv/pipx install works today
  -> awf setup checks machine + saves config/refs
  -> awf start starts Core through existing bootstrap
  -> awf init <repo> writes or validates .awf/workspace.yml
  -> awf setup --client codex/claude installs MCP config
```

The `curl | bash` installer is valuable, but it is the last slice because it
depends on trustworthy release artifacts. Do not block the CLI and setup
semantics on the marketing-friendly installer URL.

## Developer Experience Review Lock

Review date: 2026-05-28.

Product type: CLI tool + local platform + MCP/developer-agent integration +
documentation.

Primary persona:

```text
TARGET DEVELOPER PERSONA
========================
Who:       Skeptical platform/product engineer evaluating AWF locally.
Context:   They saw the repo or install command and want proof that AWF can
           safely run coding agents against a real repository.
Tolerance: 5 minutes to visible proof; 15 minutes only if each step is clearly
           progressing and recoverable.
Expects:   Docker checks, GitHub/provider guidance, one obvious next command,
           no hidden secret writes, and a source-checkout path because AWF is OSS.
```

Secondary persona:

```text
OSS CONTRIBUTOR / SOURCE-CHECKOUT EVALUATOR
===========================================
Who:       Developer who clones the AWF GitHub repo before trusting an installer.
Context:   They want to inspect the code, run AWF from the checkout, and maybe
           contribute fixes.
Tolerance: Slightly higher than the primary persona, but they still expect
           `git clone`, `uv ...`, `awf setup`, and `awf start` to work without
           reverse-engineering local service internals.
Expects:   `uv tool install . --force` and `uv run ... awf ...` paths, source
           asset detection, dev validation commands, and docs that do not imply
           PyPI/curl/Homebrew are required.
```

Correction / clarification:

- A plain `git clone` cannot by itself place an `awf` executable on `PATH`.
  The source-checkout path still needs a runner or local install step.
- The plan must support both:
  - `uv tool install . --force`, then `awf setup --source-checkout .`;
  - `uv run --python 3.12 --extra dev awf setup --source-checkout .` without a
    global `awf` install.
- This path is not only for contributors. It is also the inspectable trust path
  for security-conscious evaluators who prefer source before installer scripts.

### Target First-Run Lanes

All four install lanes must converge on the same command grammar:

```text
LANE A: release install
  curl -fsSL https://aira.pro/awf/install.sh | bash
  awf setup
  awf start
  cd <repo> && awf init .

LANE B: Python tool install
  uv tool install agent-workspace-fabric
  awf setup
  awf start
  cd <repo> && awf init .

LANE C: source checkout / inspectable OSS path
  git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
  cd aira-agent-workspace-fabric
  uv tool install . --force
  awf setup --source-checkout .
  awf start --source-checkout .
  cd <repo> && awf init .

LANE D: no global install from source checkout
  git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
  cd aira-agent-workspace-fabric
  uv run --python 3.12 --extra dev awf setup --source-checkout .
  uv run --python 3.12 --extra dev awf start --source-checkout .
```

`--source-checkout` may default to the current working directory when AWF can
verify the checkout markers, but the explicit flag must exist so docs and
automation can be unambiguous.

### Developer Empathy Narrative

I open the AWF README and see "industrial workspace fabric for AI coding
agents." That sounds powerful, but I do not yet know whether I can trust it on
my laptop. I scroll to Installation and see `uv tool install`, `pipx install`,
and a contributor `git clone` path. Good: this is inspectable. Then the current
docs say `awf init` bootstraps the service, but the new plan changes that to
`awf setup` plus `awf start`. If the docs and CLI help drift during that switch,
I will lose confidence immediately. My first real question is: "Can I prove AWF
without giving it every provider token and without installing from a remote
script?" The plan needs to answer: yes, clone the repo, run from source, perform
dry-run setup, start the local Core, and run a mocked/local smoke so I can see
AWF's control plane working before I hand it real PR authority.

### Competitive DX Benchmark

```text
Tool              | TTHW target     | Notable DX choice
------------------|-----------------|-----------------------------------------------
Claude Code       | ~15 min day one | Native installer, then guided sign-in/change
Codex CLI         | few minutes     | One global install command, then auth
Vercel CLI        | few minutes     | One CLI install, project root command, URL output
Docker hello-world| under 2 min     | One verification command prints why it worked
Hermes Agent      | few minutes     | One-line installer plus pipx fallback and doctor
Grok Build        | under 2 min     | One-line installer drops into local agent prompt
AWF target        | 2-5 min proof   | Install/source lane -> setup dry-run/start -> smoke
```

Target tier: **Competitive** for full local Core proof, with a **Champion**
sub-goal for dry-run setup. AWF has heavier Docker/provider prerequisites than
simple CLIs, so the right standard is not "PR monitor merged in 2 minutes"; it
is "developer sees a truthful local proof in one terminal before minute 5."

### Magical Moment Specification

The magical moment is not merely seeing `awf --help`. It is seeing AWF prove:

```text
AWF Core ready
API:     http://127.0.0.1:<port>
Console: http://127.0.0.1:<console-port>
Docker: ok
GitHub: ready / missing with fix
Codex:  ready / missing with fix
Next:    cd <your repo> && awf init .
Proof:   awf smoke run --mocked-local --format pretty
```

Implementation requirements:

- `awf setup --dry-run` must print an honest readiness summary without writing
  secrets or starting Core.
- `awf start` must end with an operator-readable success panel and the next
  command.
- The first proof path must not require a paid provider token or GitHub write
  access. Use mocked-local smoke or the maintained AWF Core demo project for
  first proof, then graduate to provider-backed PR creation.
- The source-checkout lane must show the same magical moment as the released
  install lane.

### Developer Journey Map

```text
STAGE        | DEVELOPER DOES                         | FRICTION POINTS              | PLAN STATUS
-------------|-----------------------------------------|------------------------------|------------
Discover     | Reads README / Quickstart               | AWF sounds powerful but heavy | Add 4-lane quickstart
Install      | Chooses curl, uv/pipx, or git clone     | Source path under-specified   | Make source lane first-class
Setup        | Runs awf setup                          | Credentials can be scary      | Refs only, keychain/env/plain choice
Hello World  | Runs awf start + mocked smoke           | Real providers may block proof| Add no-token proof path
Real Usage   | Runs awf init . then workspace/adopt PR | Needs project profile clarity | Reuse project onboarding
Debug        | Uses doctor/status/reason catalog       | Error text must be actionable | Require problem/cause/fix/docs
Upgrade      | Re-runs installer or uv/pipx/source     | Source vs release drift risk  | Add upgrade matrix per install lane
```

### First-Time Developer Confusion Report

```text
FIRST-TIME DEVELOPER REPORT
============================
Persona: skeptical platform/product engineer
Attempting: local AWF evaluation from source

CONFUSION LOG:
T+0:00  I open README. I understand the vision, but the product is complex.
T+0:45  I find install paths. I prefer git clone so I can inspect the code.
T+1:30  I run uv tool install . --force. I now have awf, but I need to know
        whether setup will use this checkout or bundled/package assets.
T+2:00  I run awf setup --dry-run. I expect exact Docker/GitHub/provider status
        and no secret mutation.
T+3:00  I run awf start. I expect one success panel, API/console URLs, and a
        smoke command that works without paying a provider.
T+5:00  If I see a local Core health proof and a next command for my repo, I
        keep going. If I hit stale docs around awf init/setup/start, I stop.
```

Confusion points addressed in this plan: source-checkout lane, dry-run setup,
no-token proof, setup/start/init naming, and action-oriented errors.

### DX Review Findings

- [P1] (confidence: 9/10) Source checkout must be a first-class install lane,
  not a contributor footnote. Current docs support `uv tool install . --force`,
  but this plan now requires `awf setup --source-checkout .` and
  `uv run ... awf setup` paths.
- [P1] (confidence: 8/10) The first proof must not require GitHub write access
  or a paid LLM provider token. `awf setup` can collect credentials later; the
  first success moment should use dry-run readiness plus mocked-local smoke.
- [P1] (confidence: 8/10) The docs must present command lanes as a matrix, not
  as scattered install snippets. Developers should know which lane they are on
  and how to upgrade/uninstall from that lane.
- [P2] (confidence: 8/10) Every `awf setup` and `awf start` failure needs a
  short problem/cause/fix/docs shape in pretty output and stable `reason_code`
  in JSON output.
- [P2] (confidence: 7/10) The console URL should use `127.0.0.1` in first-run
  output and docs where possible, matching local browser expectations and the
  current user-facing environment.

### DX Scorecard

```text
+====================================================================+
|              DX PLAN REVIEW - SCORECARD                            |
+====================================================================+
| Dimension            | Score  | Target |
|----------------------|--------|--------|
| Getting Started      | 8/10   | 10/10  |
| API/CLI/SDK          | 9/10   | 10/10  |
| Error Messages       | 8/10   | 10/10  |
| Documentation        | 8/10   | 10/10  |
| Upgrade Path         | 7/10   | 10/10  |
| Dev Environment      | 9/10   | 10/10  |
| Community            | 7/10   | 10/10  |
| DX Measurement       | 7/10   | 10/10  |
+--------------------------------------------------------------------+
| TTHW                 | 2-5 min proof target                         |
| Competitive Rank     | Competitive, Champion for dry-run setup       |
| Magical Moment       | designed via setup/start/smoke success panel  |
| Product Type         | CLI + local platform + MCP integration        |
| Mode                 | DX POLISH                                    |
| Overall DX           | 8/10 after this review                       |
+====================================================================+
```

### DX Implementation Checklist

```text
[ ] README and Quickstart show four lanes: curl, uv/pipx, source install, source no-global.
[ ] `awf setup --source-checkout .` detects and records checkout asset source.
[ ] `uv run --python 3.12 --extra dev awf setup --source-checkout .` works from clone.
[ ] `awf start --source-checkout .` uses checkout Compose/Docker assets.
[ ] First proof path works without GitHub write token or paid LLM provider.
[ ] First-run output uses 127.0.0.1 URLs for API/console where applicable.
[ ] Every first-run error includes problem, cause, fix, docs link, and reason_code.
[ ] Docs and tests reject stale no-path `awf init` bootstrap language.
[ ] Upgrade docs explain release install, uv/pipx, source checkout, and future brew.
[ ] DX smoke measures time from install/source checkout to first local proof.
```

## Key Changes

- Ship a production-grade installer at `https://aira.pro/awf/install.sh`.
  - Supports macOS and Linux first.
  - Supports `--dry-run`, `--version`, `--channel`, `--method`, and `--uninstall`.
  - Installs from pinned PyPI/GitHub release artifacts, verifies release
    metadata/checksums, and prefers
    `uv tool install agent-workspace-fabric==<version>`.
  - Keeps `uv tool install` and `pipx install` as documented first-class manual paths.
  - Defers public Homebrew advertising until a tagged artifact passes formula
    audit and `brew test`.

- Keep GitHub source checkout as a first-class install/setup lane.
  - Supports `git clone`, `uv tool install . --force`, and
    `awf setup --source-checkout .`.
  - Supports no-global-install contributor/evaluator usage through
    `uv run --python 3.12 --extra dev awf setup --source-checkout .`.
  - Source-checkout setup uses repository-local Compose, Dockerfile, migration,
    docs, and example assets after verifying AWF source markers.
  - This lane is documented as an inspectable OSS trust path, not only a
    contributor workflow.

- Add `awf setup` as the canonical one-time machine wizard.
  - Checks Docker, Compose, Git, `gh`, Python/runtime requirements, ports, disk,
    and local capacity.
  - Configures AWF local settings under `~/.awf/config.yml`.
  - Uses keychain-first credential storage: macOS Keychain and Linux Secret
    Service/libsecret when available.
  - On headless Linux with no keychain, allows raw `chmod 600` files under
    `~/.awf` only after explicit opt-in and warning.
  - Records provider credential refs and readiness status for GitHub, AWF Cloud
    stub, OpenAI/Codex, Claude/Anthropic, Ollama/OpenCode, Gemini, and future
    Antigravity/Cursor/Grok Build slots.

- Add `awf start` as the canonical local Core launcher.
  - Starts/restarts local Postgres, migrations, API, worker, and agent runtime image.
  - Reuses existing `awf service bootstrap` internals.
  - Prints health, console URL, API URL, provider readiness summary, and
    actionable next commands.
  - Keeps `awf service bootstrap/status/doctor` as expert/debug commands.

- Hard switch `awf init` to project onboarding.
  - `awf init <repo>` remains the guided/silent `.awf/workspace.yml` flow.
  - `awf init` with no path exits with a clear message: use `awf setup` for
    machine setup or `awf start` to launch Core.
  - No-path `awf init` compatibility is not preserved as canonical behavior
    because AWF is still alpha.

- Add agent-client integration to `awf setup`.
  - MCP configuration for Claude Code and Codex where supported.
  - Optional AWF-specific `SKILL.md` / agent-instruction installation for
    CLI/API fallback.
  - MCP must not accept or return raw secrets.
  - Add MCP tools for setup status, starting local Core, and project profile initialization.

- Keep console dashboard-only.
  - `awf start` may print the console URL.
  - Do not build a browser setup wizard in this slice.

## Public Interfaces

- New CLI:
  - `awf setup [--provider ...] [--client ...] [--dry-run] [--non-interactive] [--allow-plain-secrets] [--source-checkout PATH] [--format json|pretty]`
  - `awf start [--rebuild] [--skip-agent-runtime-build] [--timeout-seconds N] [--source-checkout PATH] [--format json|pretty]`
  - `awf init <repo>` only; no path is an error with migration guidance.
  - `--source-checkout PATH` points setup/start at repository-local AWF assets
    after validating source markers such as `pyproject.toml`, `src/awf/`,
    Compose/bootstrap assets, and release/docs fixtures. It may default to
    `.` only when the current directory is a verified AWF checkout.

- New local config:
  - `~/.awf/config.yml`
  - Stores non-secret settings: install channel, API host port, work dir,
    provider refs, client integration status, consent flags, and optional
    source-checkout asset path metadata.
  - Secret values live in OS keychain when available.
  - Plain files are opt-in only and must be redacted from doctor/support bundles.

- New MCP/SKILL surface:
  - `awf_get_setup_status`
  - `awf_start_local_service`
  - `awf_initialize_project_profile`
  - `awf_get_client_integration_instructions`
  - Agent guidance says: start AWF, initialize repo profile, run smoke, then
    create/adopt workspaces.

## Error And Failure Map

```text
install.sh
  unsupported OS/arch        -> fail before mutation, print supported targets
  missing curl/tar/sh tools  -> fail with exact dependency
  checksum/signature mismatch-> abort install, never run artifact
  install method fails       -> preserve logs, suggest uv/pipx/manual fallback
  PATH not writable          -> install succeeds only if executable is reachable or prints exact shell fix

awf setup
  Docker missing/offline     -> fail setup readiness, do not start Core
  source checkout invalid    -> fail with SOURCE_CHECKOUT_INVALID and exact missing marker
  keychain unavailable       -> offer env refs or explicit plain-file opt-in
  provider auth fails        -> mark provider unavailable, do not block other providers
  MCP config conflict        -> show diff/backup path, require confirmation
  client config write fails  -> leave AWF config intact, report client-specific failure
  non-interactive secret need-> fail with INTERACTIVE_INPUT_REQUIRED and next actions

awf start
  port conflict              -> suggest free port and config command
  Compose assets missing     -> fail with package/source asset diagnostic
  source assets stale        -> fail with SOURCE_CHECKOUT_ASSETS_STALE and validation command
  image build fails          -> named DockerBuildFailed-style reason
  migration fails            -> stop API/worker start, surface Alembic stderr summary
  health timeout             -> print API/worker/postgres status and log command

awf init <repo>
  no repo path               -> usage error pointing to setup/start
  existing profile           -> do not overwrite unless existing force semantics allow
  detector uncertain         -> guided choice or JSON diagnostic, not silent bad config
```

## Diagrams

```text
INSTALL
  curl install.sh
      |
      v
  manifest/version/checksum
      |
      v
  uv/pipx/PyPI artifact install
      |
      v
  awf command on PATH

SOURCE CHECKOUT
  git clone AWF
      |
      +--> uv tool install . --force -> awf setup --source-checkout .
      |
      +--> uv run --python 3.12 --extra dev awf setup --source-checkout .
      |
      v
  verified checkout assets available to setup/start

FIRST RUN
  awf setup
      |
      +--> system checks: Docker, Git, gh, ports, disk
      +--> provider setup: keychain/env/plain-opt-in refs
      +--> client setup: MCP + optional SKILL.md/instructions
      v
  ~/.awf/config.yml + credential refs

LOCAL CORE
  awf start
      |
      +--> compose postgres
      +--> migrations
      +--> API + worker
      +--> agent runtime image
      v
  health ok + console URL

PROJECT
  cd repo
  awf init .
      |
      v
  .awf/workspace.yml
      |
      v
  awf smoke / workspace create / MCP orchestration
```

## Architecture Review Adjustments

Review findings to carry into implementation:

- [P1] (confidence: 9/10) The original plan was one large cross-cutting slice.
  It is now locked as staged PRs so release automation, credentials, MCP, and
  CLI semantics can be reviewed independently.
- [P1] (confidence: 9/10) GitHub must be a first-class provider in setup, not
  only a prerequisite check. AWF cannot create, monitor, or merge PRs without a
  GitHub auth path.
- [P1] (confidence: 8/10) Credential storage needs an explicit backend
  abstraction. Keychain-first is correct, but tests need fake backends and the
  config must store refs only.
- [P2] (confidence: 8/10) Client MCP config writes are a user-trust boundary.
  `awf setup --client ...` must show a diff, write backups, support dry-run,
  and prefer official client CLIs when available.
- [P2] (confidence: 8/10) Large-file risk is real in this repo. Keep new setup
  behavior in `src/awf/host_setup/`, `src/awf/cli/setup_commands.py`,
  `src/awf/cli/start_commands.py`, and `src/awf/mcp/setup_tools.py` instead of
  extending the near-threshold files.

Search check:

- [Layer 1] `uv tool install` and `pipx install` are established isolated
  Python CLI install mechanisms. Keep both documented and tested.
- [Layer 1] Python `keyring` is the boring abstraction over macOS Keychain and
  Linux Secret Service/KWallet. Use it rather than hand-rolling OS-specific
  credential code in AWF.
- [Layer 1] Claude Code and Codex both support MCP configuration through CLI or
  config files. Prefer the client CLI when present; direct file mutation is the
  fallback with parser-backed writes, backups, and conflicts.
- [Layer 3] The installer should download and verify a pinned artifact instead
  of asking `uv` to install mutable latest from an index. This is stricter than
  the usual Python CLI quickstart, but AWF is a tool that will handle source
  code, credentials, Docker, and PR automation, so the trust bar is higher.

## Code Quality Guardrails

- Do not add credential capture logic to `provider_readiness.py`; keep readiness
  and setup separate. Readiness answers "is it usable?", setup answers "how do
  we collect and store a safe ref?"
- Do not make `awf start` a second bootstrap implementation. It should translate
  friendly flags into `ServiceBootstrapOptions` and render friendlier output.
- Do not mutate Claude/Codex config files with ad hoc string replacement. Use
  the official client command where available, or use a structured JSON/TOML
  parser with backups and conflict detection.
- Keep all operator-facing errors reason-coded and JSON-renderable before
  adding pretty rendering.
- Add docs tests in the same PR as the `awf init` hard switch so stale
  `awf init` bootstrap instructions cannot survive.

## Test Coverage Map

```text
CODE PATHS                                      USER FLOWS
[+] awf setup                                   [+] Fresh machine setup
  +-- [GAP] system checks                         +-- [GAP] Docker missing/offline
  +-- [GAP] config read/write                     +-- [GAP] GitHub via gh/env ref
  +-- [GAP] source-checkout validation            +-- [GAP] source install/no-global lanes
  +-- [GAP] keyring backend                       +-- [GAP] OpenAI/Claude/Gemini/Ollama refs
  +-- [GAP] env-ref backend                       +-- [GAP] headless Linux plain-file refusal
  +-- [GAP] plain-file opt-in                     +-- [GAP] non-interactive missing secret
  +-- [GAP] provider readiness summary

[+] awf start                                  [+] Local Core start/restart
  +-- [EXISTING] service bootstrap internals      +-- [EXISTING] bootstrap success/failure
  +-- [GAP] friendly wrapper flags                +-- [GAP] awf start health output
  +-- [GAP] source asset selection                 +-- [GAP] source checkout Core start
  +-- [GAP] provider summary rendering            +-- [GAP] port conflict next action

[+] awf init <repo> only                       [+] Project onboarding
  +-- [EXISTING] project preview/write            +-- [EXISTING] guided profile setup
  +-- [GAP] no-path migration error               +-- [GAP] old docs rejected by tests

[+] MCP setup tools                            [+] Agent client orchestration
  +-- [GAP] setup status tool                     +-- [GAP] agent starts AWF via MCP
  +-- [GAP] start local service tool              +-- [GAP] agent initializes repo profile
  +-- [GAP] initialize project profile tool       +-- [GAP] tool responses redact secrets
  +-- [GAP] client instructions tool

[+] installer                                  [+] Released install
  +-- [GAP] manifest resolve                      +-- [GAP] dry-run inspectable install
  +-- [GAP] sha256 verification                   +-- [GAP] pinned channel/version install
  +-- [GAP] uv/pipx method fallback               +-- [GAP] uninstall AWF-managed files only
  +-- [GAP] PATH repair advice

COVERAGE TARGET: every GAP above gets behavior + edge + error coverage.
E2E TARGET: released wheel install from outside checkout, setup dry-run,
source checkout install, source no-global run, start help, init help, MCP
server help.
```

Required focused tests:

- `tests/unit/cli/test_setup_commands.py`
  - system checks pass/fail without starting Core;
  - `--source-checkout .` validates AWF source markers and records source
    asset metadata;
  - invalid `--source-checkout` path returns `SOURCE_CHECKOUT_INVALID` with the
    missing marker;
  - config writes refs only;
  - keyring fake backend success/failure;
  - env-ref provider setup;
  - headless Linux plain files require `--allow-plain-secrets`;
  - non-interactive secret capture fails with `INTERACTIVE_INPUT_REQUIRED`;
  - stdout/stderr never contain token values.
- `tests/unit/cli/test_start_commands.py`
  - `awf start` delegates to existing bootstrap options;
  - `awf start --source-checkout .` passes verified source asset locations into
    bootstrap options;
  - stale or missing source assets produce reason-coded diagnostics and do not
    silently fall back to package assets;
  - structured bootstrap errors are preserved;
  - pretty output includes health, console/API URLs, provider summary, and next
    commands.
- Existing `tests/unit/cli/test_init_parts/*`
  - no-path `awf init` exits with setup/start migration guidance;
  - project-path `awf init <repo>` behavior remains unchanged;
  - bootstrap-only flags are rejected or removed consistently.
- `tests/unit/service/test_host_setup_config.py`
  - `~/.awf/config.yml` schema round-trips;
  - permissions are set conservatively;
  - corrupt config yields reason-coded diagnostics.
- `tests/unit/service/test_host_setup_credentials.py`
  - keyring, env-ref, and plain-file backends;
  - backend unavailable paths;
  - redaction and support-bundle behavior.
- `tests/unit/mcp/test_setup_tools.py`
  - setup status returns refs/status only;
  - start tool is idempotent and reports structured failures;
  - project init tool uses the same onboarding writer as CLI;
  - client integration instructions include no secrets.
- `tests/unit/installer/test_install_script.py`
  - `bash -n` syntax check;
  - dry-run for macOS/Linux fixture environments;
  - unsupported OS/arch fails before mutation;
  - checksum mismatch aborts;
  - uninstall refuses unmanaged executables;
  - zsh/bash/fish PATH advice.
- Release/package tests:
  - built wheel includes bootstrap assets and new installer metadata;
  - clean install from outside checkout runs `awf --help`, `awf setup --help`,
    `awf start --help`, `awf init --help`, and `awf mcp serve --help`;
  - source checkout lane runs `uv tool install . --force`, then
    `awf setup --source-checkout . --dry-run` from the checkout;
  - no-global source lane runs
    `uv run --python 3.12 --extra dev awf setup --source-checkout . --dry-run`;
  - publish workflow emits `awf-install-manifest.json` and checksums.

## Failure Modes To Prove

| Flow | Realistic failure | Expected behavior | Test required |
| --- | --- | --- | --- |
| installer | manifest checksum mismatch | abort before install, print reason and artifact URL | yes |
| installer | PATH target not reachable | install does not claim success unless `awf` is executable or shell fix is printed | yes |
| setup | Docker not installed or daemon stopped | setup readiness fails, no Core start attempted | yes |
| setup | source checkout path is not AWF source | fail with `SOURCE_CHECKOUT_INVALID`, name the missing marker, and suggest release install or correct path | yes |
| setup | keychain backend unavailable on headless Linux | offer env refs or plain opt-in, no raw secret write by default | yes |
| setup | provider auth invalid | mark only that provider unavailable, continue remaining providers | yes |
| setup | MCP config conflict | show diff and backup path, require confirmation before write | yes |
| start | source checkout assets missing or stale | fail with source asset diagnostic and validation command, no silent package fallback | yes |
| start | migration failure | stop startup path and surface Alembic stderr summary | existing + wrapper test |
| init | no path after hard switch | exit code 2 with `awf setup` and `awf start` guidance | yes |
| MCP | tool response includes credential ref | redact sensitive fields and never include raw secret values | yes |

No critical silent gaps are acceptable: every failure above must either have a
clear terminal message or a structured JSON `reason_code`.

## Performance Review

- `awf setup --dry-run` should finish quickly on a normal machine because it
  performs local binary/config checks and bounded subprocess probes only.
- Provider network checks must be bounded and opt-in or provider-selected. A
  missing Gemini/OpenAI/Claude network response should not make the whole setup
  feel hung.
- Reuse existing provider readiness timeouts instead of adding unbounded HTTP
  calls.
- Client config detection should read a small set of known files only. Do not
  recursively scan the user's home directory.
- Installer verification should stream/download to a temporary file and hash
  incrementally rather than loading large artifacts into memory.

## Worktree Parallelization Strategy

| Step | Modules touched | Depends on |
| --- | --- | --- |
| CLI foundation | `src/awf/cli`, `src/awf/host_setup`, docs | none |
| credentials/providers | `src/awf/host_setup`, provider tests | CLI foundation config schema |
| MCP/client integration | `src/awf/mcp`, `src/awf/host_setup`, docs | CLI foundation config schema |
| installer/release | `packaging`, `scripts`, `.github/workflows`, release docs | none, but final smoke depends on CLI names |

Parallel lanes:

- Lane A: CLI foundation -> credentials/providers.
- Lane B: MCP/client integration after the config schema is stable.
- Lane C: installer/release can start in parallel after CLI names are fixed.

Execution order:

1. Land CLI foundation first to lock public command names and config schema.
2. Run credentials/providers and MCP/client integration in parallel worktrees.
3. Run installer/release in parallel once command names are stable.
4. Merge all and run the release install smoke from outside the checkout.

Conflict flags:

- Credentials/providers and MCP/client integration both touch
  `src/awf/host_setup`; keep contracts small and shared through the config
  schema.
- CLI foundation and docs tests will intentionally touch many public docs; avoid
  parallel doc edits until command names are locked.

## Test Plan

- Installer tests:
  - dry-run on macOS/Linux fixtures;
  - unsupported OS/arch fails before mutation;
  - checksum mismatch aborts;
  - pinned version/channel resolution;
  - uninstall removes only AWF-managed files;
  - PATH advice is correct for zsh/bash/fish.

- CLI tests:
  - `awf setup` writes config without logging secrets;
  - `awf setup --source-checkout . --dry-run` works from a cloned AWF checkout;
  - `uv run --python 3.12 --extra dev awf setup --source-checkout . --dry-run`
    works without a global `awf` install;
  - keychain provider success/failure with fake backends;
  - headless Linux plain-file secret storage requires explicit opt-in;
  - `awf start` calls bootstrap internals and reports health;
  - `awf start --source-checkout .` selects checkout assets and fails clearly
    when checkout assets are incomplete;
  - `awf init` no path errors with setup/start guidance;
  - `awf init <repo>` behavior remains unchanged.

- MCP and client integration tests:
  - setup status tool never returns secret values;
  - start tool is idempotent and reports structured failures;
  - project init tool writes/preview profiles through the same onboarding writer;
  - client integration instructions tool emits Claude/Codex config without secrets;
  - Claude/Codex config writers use backups and conflict detection.

- Release and package tests:
  - built wheel includes installer-visible bootstrap assets;
  - clean venv install from outside checkout runs `awf --help`,
    `awf setup --help`, `awf start --help`, `awf init --help`;
  - source checkout smoke covers `git clone`, `uv tool install . --force`,
    `awf setup --source-checkout . --dry-run`, and `awf start --help`;
  - publish workflow emits checksums/manifest artifacts;
  - docs tests confirm install/setup/start/init naming.

- Security tests:
  - support bundle redacts keychain refs and plain-file paths where needed;
  - no raw provider tokens in stdout/stderr/logs/config snapshots;
  - MCP cannot write or read raw provider credentials;
  - installer refuses mutable/unverified artifact metadata.

- Documentation/DX tests:
  - README and Quickstart render four first-run lanes: curl, uv/pipx, source
    install, and source no-global;
  - first-run examples use `127.0.0.1` for API/console URLs where applicable;
  - docs include an upgrade/uninstall path for each install lane;
  - docs test fails if no-path `awf init` is described as service bootstrap.

## NOT In Scope

- AWF Cloud backend implementation. Only reserve the setup slot and config vocabulary.
- Native Windows installer. Windows can be documented as future or WSL-only for now.
- Public Homebrew install path before tagged PyPI/GitHub artifacts and formula audit.
- Making plain `git clone` alone create an `awf` command. Source checkout is
  first-class, but it still requires `uv tool install . --force` or
  `uv run --python 3.12 --extra dev awf ...`.
- Browser-based setup wizard. Console remains dashboard-only.
- Automatic Docker Desktop installation. Setup detects and guides; it does not install Docker.
- Per-provider full OAuth implementation unless delegated to existing provider CLIs.
- Remote hosted MCP for AWF Cloud. This slice configures local stdio MCP only.
- MCP-based credential entry. Provider secrets are captured only through the
  terminal CLI or existing provider CLIs/env refs.
- Full encrypted plain-file secret backend. Plain files are a warned,
  opt-in fallback only until a stronger Linux/headless story exists.
- Native package managers beyond PyPI/uv/pipx and the install script. Homebrew
  remains a follow-up after release artifact audit.

## Assumptions

- AWF is alpha, so a hard switch from no-path `awf init` to `awf setup` /
  `awf start` is acceptable.
- `~/.awf` is AWF's local config home, not the default raw secret store.
- macOS and Linux are the supported first release targets.
- `uv tool install` and `pipx install` remain supported even after the curl
  installer ships.
- The installer and setup flow must be safe enough for a security-conscious
  developer to inspect, dry-run, pin, and uninstall.
- Source-checkout setup is a supported user path, not only a contributor path,
  because OSS evaluators often trust inspectable source before remote installers.

## Review Completion Summary

- Step 0: scope accepted with staged PR split.
- DX review: completed; source-checkout lane, no-token proof, 127.0.0.1
  first-run output, upgrade matrix, and DX smoke requirements added.
- Architecture review: 5 issues found and folded into this plan.
- Code quality review: 5 guardrails added.
- Test review: coverage map produced; 30+ concrete gaps mapped to required
  tests.
- Performance review: bounded setup, provider, client-config, and installer
  constraints added.
- NOT in scope: expanded and explicit.
- What already exists: documented so implementation reuses current bootstrap,
  status, doctor, provider readiness, project onboarding, MCP setup docs, and
  release workflow.
- Failure modes: 11 user-visible failure paths mapped to expected behavior and
  tests.
- Outside voice: skipped; this is a plan-only local engineering review.
- Parallelization: 3 lanes, with CLI foundation first and installer/release
  partly parallel.
- Lake score: complete path selected for security, tests, and distribution;
  implementation is staged rather than reduced.

## GSTACK REVIEW REPORT

| Review | Skill | Purpose | Runs | Status | Findings |
| --- | --- | --- | --- | --- | --- |
| CEO Review | `/plan-ceo-review` | Founder/product scope | 1 | clean | 8 proposals, 7 accepted, 1 deferred |
| Codex Review | `/codex review` | Independent implementation critique | 0 | not run | Plan-only review, no product diff |
| Eng Review | `/plan-eng-review` | Architecture, tests, failure modes | 1 | clean | 44 findings folded into plan, 0 unresolved |
| Design Review | `/plan-design-review` | Visual/UI experience | 0 | not run | No UI design slice in this plan |
| DX Review | `/plan-devex-review` | Developer experience and onboarding | 1 | clean | Overall DX 8/10, target TTHW 2-5 min, source-checkout lane locked |

Verdict: CEO, Eng, and DX reviews are clean. The plan is ready for
implementation sequencing, but the implementation should still land in the
staged PRs defined above.
