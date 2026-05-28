# AWF Full Installer And First-Run Setup Backlog

Generated: 2026-05-28

Source plan: `plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md`

Review mode: `plan-eng-review` execution breakdown. This file turns the reviewed
plan into workspace-sized implementation tasks and an execution graph that
respects an eight-workspace local capacity cap.

## Operating Rules

- Do not launch a task until every listed dependency is merged or explicitly
  satisfied by the human operator.
- Each implementation task still needs its own `plans/<TOPIC>_PLAN.md` and
  `plans/<TOPIC>_VALIDATION.md` before coding, per `AGENTS.md`.
- Prefer one PR per task unless a task is marked `human` or `coordination`.
- Use `auto_merge=true` for AWF PR monitors targeting `development`.
- Keep new setup code out of existing near-threshold modules. Prefer
  `src/awf/host_setup/`, `src/awf/cli/setup_commands.py`,
  `src/awf/cli/start_commands.py`, and `src/awf/mcp/setup_tools.py`.
- Every first-run user-visible error must have a stable reason code, pretty
  problem/cause/fix/docs output, and JSON output.
- Every task must include behavior, edge, and error tests for its new codepaths.

## Locked Human Decisions

- H01 installer hosting and trust: GitHub Releases is the canonical artifact
  and manifest source for v1. `aira.pro` may serve or redirect `install.sh`
  and can mirror release artifacts later. v1 requires manifest-pinned `sha256`
  verification. Reserve signature fields for a later signing slice, but do not
  block v1 on release signing infrastructure.
- H02 plain-secret policy: allow `chmod 600` plain-file provider secrets only
  for Linux/headless setups after explicit warning and consent. Keyring and env
  refs remain preferred.
- H03 execution model: use `codex` with `gpt-5.5` and `xhigh` reasoning effort
  for all AWF implementation workspaces in this backlog.
- H04 launch preflight: before launching implementation workspaces, clean
  expired AWF resources, rebuild local service images, and rerun AWF bootstrap.

H01 through H04 are complete for scheduling purposes. Later H01/H02 dependency
references keep the graph traceable, but they are not additional human approval
holds unless a human operator explicitly reopens one of the decisions above.

## What Already Exists

These are reuse targets, not work to rebuild:

- `awf service bootstrap` starts local Postgres, migrations, API, worker, agent
  runtime image, and returns structured bootstrap failures.
- `awf service status` and `awf service doctor` already collect Docker, API,
  worker, provider, disk, capacity, and support-bundle diagnostics.
- `src/awf/service/provider_readiness.py` already probes GitHub, Codex, Claude
  Code, Gemini, OpenCode/Ollama, Docker, token redaction, and bounded provider
  readiness.
- `awf init <path>` and `awf profile init` already handle project profile
  onboarding.
- `docs/MCP_SETUP.md` already documents Claude Code and Codex stdio setup for
  `awf mcp serve`.
- `.github/workflows/publish.yml` and `RELEASING.md` already build Python
  distributions and checksum artifacts.
- Existing public docs and docs tests already guard large parts of the first-run
  developer experience.

## NOT In Scope

- AWF Cloud backend implementation.
- Native Windows installer, except WSL or future docs wording.
- Public Homebrew install advertising before tagged artifacts and formula audit.
- Browser setup wizard in the console.
- Automatic Docker Desktop installation.
- Full per-provider OAuth implementation beyond existing provider CLIs or env
  refs.
- Hosted remote MCP for AWF Cloud.
- MCP-based credential entry. Provider secrets stay in terminal CLI flows.
- Encrypted plain-file secret backend. Plain files are warned, opt-in fallback.
- Making `git clone` alone create an `awf` executable on `PATH`.

## Task Backlog

| ID | Title | Owner type | Status | Priority | Depends on | Parallel group |
| --- | --- | --- | --- | --- | --- | --- |
| H01 | Decide public installer hosting and release trust contract | human | done - locked | P0 | - | human |
| H02 | Decide first-run provider copy and plain-secret consent wording | human | done - locked | P1 | - | human |
| T01 | Lock public CLI grammar and hard-switch no-path `awf init` | workspace | planned | P0 | - | foundation |
| T02 | Add host setup config schema and source-checkout asset model | workspace | planned | P0 | - | foundation |
| T03 | Add first-run error contract and rendering helpers | workspace | planned | P0 | T02 | foundation |
| T04 | Add `awf setup --dry-run` system checks and readiness payload | workspace | planned | P0 | T01, T02, T03 | setup |
| T05 | Add `awf start` wrapper over existing service bootstrap | workspace | planned | P0 | T01, T02, T03 | start |
| T06 | Add keychain/env/plain-file credential ref backends | workspace | planned | P0 | T02, T03, H02 | credentials |
| T07 | Add provider setup orchestration with GitHub first-class | workspace | planned | P0 | T04, T06 | credentials |
| T08 | Add Claude/Codex client config diff, backup, and write helpers | workspace | planned | P1 | T02, T03 | clients |
| T09 | Add setup/start/init/client MCP tools | workspace | planned | P1 | T04, T05, T08 | mcp |
| T10 | Add no-token local proof and mocked smoke path | workspace | planned | P0 | T04, T05 | smoke |
| T11 | Add install manifest generator and release metadata contract | workspace | planned | P0 | T01, H01 | release |
| T12 | Add checked-in `install.sh` with checksum verification | workspace | planned | P0 | T11, H01 | installer |
| T13 | Ensure wheel/source packages contain bootstrap and installer assets | workspace | planned | P0 | T02, T11 | packaging |
| T14 | Add clean-install and source-lane E2E smoke harness | workspace | planned | P0 | T05, T10, T12, T13 | e2e |
| T15 | Update README, Quickstart, upgrade, uninstall, and source lanes | workspace | planned | P0 | T01, T04, T05, T10, T12, T13 | docs |
| T16 | Add release workflow checks for manifest, checksums, and installer smoke | workspace | planned | P0 | T11, T12, T13, H01 | release |
| T17 | Add support-bundle and log redaction coverage for setup secrets | workspace | planned | P0 | T06, T07 | security |
| T18 | Add docs drift tests for setup/start/init command grammar | workspace | planned | P1 | T01, T15 | docs |
| T19 | Final integration, full coverage, and first-run lane validation | coordination | planned | P0 | T07, T09, T14, T15, T16, T17, T18 | final |

## Task Cards

### H01 - Decide Public Installer Hosting And Release Trust Contract

Owner type: human

Status: done - locked in [Locked Human Decisions](#locked-human-decisions)

Depends on: none

Blocks: T11, T12, T16

What:

- Decide where `https://aira.pro/awf/install.sh` and
  `awf-install-manifest.json` will be hosted.
- Decide whether the first public manifest points at GitHub Releases, an Aira
  mirror, or both.
- Decide whether the first release requires signature verification in addition
  to sha256 verification.

Acceptance criteria:

- A written decision exists in the implementation PR or release docs.
- Installer tasks know the canonical URL, fallback URL, channel names, and
  trust language.
- Any required hosting or DNS work is completed before public docs advertise
  `curl | bash`.

### H02 - Decide First-Run Provider Copy And Plain-Secret Consent Wording

Owner type: human

Status: done - locked in [Locked Human Decisions](#locked-human-decisions)

Depends on: none

Blocks: T06

What:

- Approve user-facing wording for provider credential prompts.
- Approve the warning and confirmation wording for `--allow-plain-secrets`.
- Decide whether plain-file fallback should be Linux/headless only in the first
  implementation.

Acceptance criteria:

- Setup CLI has exact approved consent copy for dangerous secret storage paths.
- Non-interactive mode can return machine-readable `INTERACTIVE_INPUT_REQUIRED`
  without relying on prose.
- Tests can assert stable prompt fragments without encoding accidental wording.

### T01 - Lock Public CLI Grammar And Hard-Switch No-Path `awf init`

Owner type: workspace

Modules touched:

- `src/awf/cli`
- `tests/unit/cli`
- docs tests that reference command grammar

Depends on: none

What:

- Register `awf setup` and `awf start` command surfaces as real Typer command
  groups or commands.
- Preserve `awf init <repo>` project onboarding.
- Change no-path `awf init` into a clear migration error that points to
  `awf setup` and `awf start`.
- Remove or reject bootstrap-only flags from the no-path init path.

Acceptance criteria:

- `awf setup --help`, `awf start --help`, and `awf init --help` are stable.
- `awf init` with no repo exits non-zero with setup/start guidance.
- `awf init <repo>` remains behavior-compatible with existing onboarding.
- Docs tests fail if no-path `awf init` is described as service bootstrap.

Required tests:

- `tests/unit/cli/test_setup_commands.py` for help and placeholder behavior.
- `tests/unit/cli/test_start_commands.py` for help and placeholder behavior.
- Existing init tests for no-path migration and project-path compatibility.

### T02 - Add Host Setup Config Schema And Source-Checkout Asset Model

Owner type: workspace

Modules touched:

- `src/awf/host_setup`
- `tests/unit/service`
- packaging tests if source asset metadata needs fixtures

Depends on: none

What:

- Add `~/.awf/config.yml` schema and IO helpers.
- Store non-secret settings only: install channel, API host port, work dir,
  provider refs, client integration status, consent flags, and optional source
  checkout asset metadata.
- Add source-checkout validation for AWF source markers such as
  `pyproject.toml`, `src/awf/`, compose/bootstrap assets, docs, and release
  fixtures.
- Add reason-coded diagnostics for corrupt config and invalid source checkout.

Acceptance criteria:

- Config read/write round-trips with conservative permissions.
- Secret values cannot be stored in config objects.
- Invalid source checkout fails with `SOURCE_CHECKOUT_INVALID` and missing
  marker details.
- Verified source checkout can be passed to setup/start without guessing.

Required tests:

- `tests/unit/service/test_host_setup_config.py`
- Source-checkout fixture tests for valid, missing marker, unreadable path, and
  stale asset metadata.

### T03 - Add First-Run Error Contract And Rendering Helpers

Owner type: workspace

Modules touched:

- `src/awf/host_setup`
- `src/awf/service/doctor`
- `docs/REASON_CATALOG.md`
- setup/start CLI tests

Depends on: T02

What:

- Add shared problem/cause/fix/docs rendering for first-run setup/start errors.
- Add JSON-safe payload models with stable `reason_code`.
- Add reason catalog entries for setup, source-checkout, installer, and
  credential failures that are introduced by this plan.

Acceptance criteria:

- Pretty output can render a concise operator-facing panel for every known
  first-run failure.
- JSON output exposes stable reason codes and structured remediation fields.
- No token-looking values are rendered in pretty or JSON output.

Required tests:

- Renderer unit tests for success, warning, and failure payloads.
- Reason catalog coverage tests for all new codes.
- Redaction tests using representative provider refs and token-shaped strings.

### T04 - Add `awf setup --dry-run` System Checks And Readiness Payload

Owner type: workspace

Modules touched:

- `src/awf/cli/setup_commands.py`
- `src/awf/host_setup/system_checks.py`
- `src/awf/host_setup/rendering.py`
- `tests/unit/cli`
- `tests/unit/service`

Depends on: T01, T02, T03

What:

- Implement `awf setup` as the one-time machine setup wizard shell.
- Support repeatable `--provider PROVIDER`, `--dry-run`, `--non-interactive`,
  `--source-checkout PATH`, and `--format json|pretty`.
- Check Docker, Compose, Git, `gh`, Python/runtime, ports, disk, shell/PATH,
  and local capacity without starting Core.
- Write safe config updates only when not in dry-run mode.

Acceptance criteria:

- `awf setup --dry-run` never writes secrets and never starts Core.
- `awf setup --provider github --dry-run` accepts and forwards the provider
  selector so later provider setup can recheck a single provider.
- Unknown provider names fail with a reason-coded setup diagnostic instead of
  silently falling back to all-provider setup.
- Missing Docker or stopped daemon returns a setup readiness failure with next
  actions.
- Source-checkout dry-run works from a cloned AWF checkout and from
  `uv run --python 3.12 --extra dev awf setup --source-checkout .`.
- Pretty output includes status, blockers, warnings, docs links, and next
  command.

Required tests:

- `tests/unit/cli/test_setup_commands.py`
- CLI parser tests for no selector, single-provider selector, repeated
  selectors, and unknown provider rejection.
- Unit tests for pass/fail system-check fixtures.
- Non-interactive secret-needed path returns `INTERACTIVE_INPUT_REQUIRED`.

### T05 - Add `awf start` Wrapper Over Existing Service Bootstrap

Owner type: workspace

Modules touched:

- `src/awf/cli/start_commands.py`
- existing service bootstrap adapters only for delegation if needed
- `tests/unit/cli`

Depends on: T01, T02, T03

What:

- Implement `awf start` as a friendly wrapper over existing local service
  bootstrap internals.
- Support `--rebuild`, `--skip-agent-runtime-build`, `--timeout-seconds`,
  `--source-checkout PATH`, and `--format json|pretty`.
- Select package assets or verified source-checkout assets explicitly.
- Preserve structured bootstrap failures.

Acceptance criteria:

- `awf start` delegates to the existing bootstrap implementation rather than
  reimplementing service startup.
- Success output includes API URL, console URL, Docker/provider summary, and
  next commands.
- Source assets missing or stale fail with a reason-coded diagnostic and do not
  silently fall back to package assets.
- Port conflict and migration failure retain useful existing diagnostics.

Required tests:

- `tests/unit/cli/test_start_commands.py`
- Bootstrap delegation tests using fake bootstrap runner.
- Source asset selection and stale source asset failure tests.

### T06 - Add Keychain/Env/Plain-File Credential Ref Backends

Owner type: workspace

Modules touched:

- `src/awf/host_setup/credentials.py`
- `src/awf/host_setup/config.py`
- support-bundle redaction hooks if needed
- `tests/unit/service`

Depends on: T02, T03, H02

What:

- Add credential backend abstraction with `keyring`, `env_ref`, and explicit
  opt-in `plain_file` support.
- Store refs and metadata in config, never raw provider values.
- Use fake credential backends in tests.
- Enforce non-interactive behavior for missing secret input.

Acceptance criteria:

- Keyring backend is default when available.
- Env ref stores only variable names such as `OPENAI_API_KEY` or `GH_TOKEN`.
- Plain file storage requires `--allow-plain-secrets` and approved consent.
- Plain file storage is rejected on non-Linux or non-headless hosts even when
  `--allow-plain-secrets` and consent are present.
- Headless Linux without keychain offers env ref or explicit plain-file opt-in.
- Raw tokens do not appear in stdout, stderr, config, logs, or test snapshots.

Required tests:

- `tests/unit/service/test_host_setup_credentials.py`
- Backend unavailable tests.
- Permission tests for any plain-file fallback.
- Non-Linux and non-headless rejection tests for the plain-file backend.
- Redaction tests for token-shaped inputs.

### T07 - Add Provider Setup Orchestration With GitHub First-Class

Owner type: workspace

Modules touched:

- `src/awf/host_setup/providers.py`
- `src/awf/service/provider_readiness.py` only for narrow integration points
- `tests/unit/service`

Depends on: T04, T06

What:

- Add provider setup orchestration for GitHub, AWF Cloud stub, OpenAI/Codex,
  Claude/Anthropic, Ollama/OpenCode, Gemini, and future provider slots.
- Treat GitHub as first-class because PR creation, monitoring, and merging
  depend on it.
- Convert captured or discovered credentials into refs consumed by provider
  readiness checks.
- Honor the setup CLI provider selector by configuring and rechecking only the
  requested provider or providers when `--provider` is supplied.
- Keep failed provider auth non-blocking for other providers.

Acceptance criteria:

- GitHub can be marked ready via `gh` or env ref without raw token storage.
- One failed provider marks only that provider unavailable.
- Provider readiness summary can be rendered by setup and start.
- Provider setup network probes are bounded.
- Selected-provider setup leaves unselected providers unchanged and labels the
  summary as a targeted recheck rather than an all-provider run.

Required tests:

- Provider success, missing credential, invalid credential, and mixed-provider
  partial readiness.
- Selected-provider tests proving one provider can be configured or rechecked
  without probing or mutating unrelated providers.
- GitHub-specific setup through `gh` and env ref.
- No raw secret values in provider summaries.

### T08 - Add Claude/Codex Client Config Diff, Backup, And Write Helpers

Owner type: workspace

Modules touched:

- `src/awf/host_setup/clients.py`
- `docs/MCP_SETUP.md`
- client integration tests

Depends on: T02, T03

What:

- Add client integration helpers for Claude Code and Codex MCP config.
- Prefer official client CLIs when present.
- Use structured JSON/TOML parsing for file fallback.
- Show diffs, write backups, detect conflicts, support dry-run, and avoid raw
  credential handling.

Acceptance criteria:

- `awf setup --client claude` and `--client codex` can produce a dry-run diff.
- Config write creates a backup and refuses ambiguous conflicts.
- Existing unrelated client config is preserved.
- Client setup never reads, accepts, or returns provider tokens.

Required tests:

- Config missing, already configured, conflicting config, write failure, backup
  creation, and dry-run no-mutation.
- Official CLI path and file-fallback path with fakes.

### T09 - Add Setup/Start/Init/Client MCP Tools

Owner type: workspace

Modules touched:

- `src/awf/mcp/setup_tools.py`
- `src/awf/mcp/server.py`
- MCP parity docs/tests

Depends on: T04, T05, T08

What:

- Add MCP tools:
  - `awf_get_setup_status`
  - `awf_start_local_service`
  - `awf_initialize_project_profile`
  - `awf_get_client_integration_instructions`
- Reuse setup/start/init/client service functions instead of duplicating logic.
- Keep raw credential capture out of MCP.

Acceptance criteria:

- MCP setup status returns refs/status only.
- MCP start tool is idempotent and reports structured failures.
- MCP project init uses the same onboarding writer as CLI.
- MCP client instructions contain no secret values.

Required tests:

- `tests/unit/mcp/test_setup_tools.py`
- MCP parity matrix/docs updates if this repo tracks MCP parity status.
- Secret redaction tests for every tool response.

### T10 - Add No-Token Local Proof And Mocked Smoke Path

Owner type: workspace

Modules touched:

- `src/awf/service/smoke.py`
- CLI smoke surfaces if needed
- docs tests

Depends on: T04, T05

What:

- Add or adapt a mocked/local smoke proof that works without GitHub write access
  or a paid LLM provider token.
- Make first-run output point to this proof after `awf start`.
- Preserve existing real-provider smoke paths for later validation.

Acceptance criteria:

- A skeptical evaluator can run a local proof after setup/start without handing
  AWF PR authority.
- Smoke output proves local Core health rather than only printing a URL.
- Existing smoke console false-green pitfall stays covered.

Required tests:

- Unit tests for mocked smoke success and failure.
- CLI/docs tests proving the first-run next command is provider-free.
- Regression coverage that local proof does not claim readiness without a real
  API/worker health signal.

### T11 - Add Install Manifest Generator And Release Metadata Contract

Owner type: workspace

Modules touched:

- `scripts/generate_install_manifest.py`
- `.github/workflows/publish.yml`
- `RELEASING.md`
- release tests

Depends on: T01, H01

What:

- Generate `awf-install-manifest.json` with version, channel, artifact URLs,
  sha256 values, platform metadata, and `generated_at`.
- Define channel semantics for stable and pre-release channels.
- Extend existing release workflow rather than building a parallel release
  process.

Acceptance criteria:

- Manifest generation is deterministic enough for tests.
- Manifest entries point at pinned artifacts, not mutable latest URLs.
- Existing wheel/sdist checksum artifacts remain intact.
- Release docs explain how to inspect and verify the manifest.

Required tests:

- Manifest generator unit tests.
- Workflow/documentation tests that required release artifacts exist.
- Fixture tests for channel/version selection.

### T12 - Add Checked-In `install.sh` With Checksum Verification

Owner type: workspace

Modules touched:

- `packaging/install.sh`
- `tests/unit/installer`
- install docs

Depends on: T11, H01

What:

- Add inspected shell installer supporting macOS and Linux first.
- Support `--dry-run`, `--version`, `--channel`, `--method`, `--install-dir`,
  `--uninstall`, and `--help`.
- Resolve manifest, download artifact, verify sha256, install via local
  verified wheel with `uv tool install`, and verify `awf` reachability.
- Implement pinned `uv tool install` or `pipx install` fallback when configured.

Acceptance criteria:

- Checksum mismatch aborts before install.
- Unsupported OS/arch fails before mutation.
- Dry-run avoids install mutation and explains planned actions.
- Uninstall removes only AWF-managed files and refuses unknown executables.
- PATH advice is correct for zsh, bash, and fish.

Required tests:

- `bash -n packaging/install.sh`
- Fixture-based dry-run tests for macOS/Linux.
- Checksum mismatch, unsupported OS/arch, install-method failure, PATH advice,
  and unmanaged uninstall refusal.

### T13 - Ensure Wheel/Source Packages Contain Bootstrap And Installer Assets

Owner type: workspace

Modules touched:

- `pyproject.toml`
- package data config
- `src/awf/host_setup`
- package tests

Depends on: T02, T11

What:

- Ensure built packages include local service compose/bootstrap assets, agent
  runtime Dockerfile assets, docs or fixture assets needed by setup/start, and
  installer-visible metadata.
- Make source-checkout asset selection and package asset selection testable from
  outside the repo checkout.

Acceptance criteria:

- Clean wheel install from outside checkout can run help for `awf`, setup,
  start, init, and MCP.
- `awf start` can locate package assets when no source checkout is configured.
- Source checkout mode remains explicit and does not mask stale package assets.

Required tests:

- Package content tests.
- Clean venv or temp install smoke from outside checkout.
- Source and package asset selection tests.

### T14 - Add Clean-Install And Source-Lane E2E Smoke Harness

Owner type: workspace

Modules touched:

- tests/integration or scripts test harness
- CI helpers if needed
- docs smoke references

Depends on: T05, T10, T12, T13

What:

- Add E2E smoke harness for the four install lanes:
  - release installer dry-run or local fixture install
  - `uv tool install agent-workspace-fabric`
  - source checkout with `uv tool install . --force`
  - source checkout with `uv run --python 3.12 --extra dev awf ...`
- Keep destructive/network parts fixture-backed unless explicitly release-gated.

Acceptance criteria:

- The harness can run locally without publishing a real release.
- Release-gated tests can be enabled in CI when artifacts exist.
- The source no-global lane proves `uv run ... awf setup --source-checkout .`
  works.

Required tests:

- E2E fixture tests for setup dry-run and help commands.
- Source checkout smoke from a temp clone or copied checkout.
- Installer dry-run smoke against fixture manifest.

### T15 - Update README, Quickstart, Upgrade, Uninstall, And Source Lanes

Owner type: workspace

Modules touched:

- `README.md`
- docs quickstart files
- `RELEASING.md`
- `docs/MCP_SETUP.md`
- docs tests

Depends on: T01, T04, T05, T10, T12, T13

Execution model:

- Single-phase final docs task, not a split skeleton/final task.
- Do not launch T15 until T01, T04, T05, T10, T12, and T13 are merged or
  explicitly satisfied; all deliverables and acceptance criteria below apply
  to the completed T15 task.
- T18 depends on completed and merged T15, not on an early docs skeleton.

What:

- Present four first-run lanes:
  - curl installer
  - uv/pipx tool install
  - source checkout with global tool install
  - source checkout with no global install
- Document setup, start, init, mocked smoke, upgrade, and uninstall paths.
- Use `127.0.0.1` for local API/console URLs where applicable.
- Remove stale no-path `awf init` bootstrap language.

Acceptance criteria:

- A first-time evaluator can pick one lane and follow only that lane.
- Docs explain which lane is inspectable and which is release-installed.
- Docs include upgrade and uninstall path for each lane.
- Docs tests reject stale command grammar.

Required tests:

- Existing public docs readability/link tests.
- Snippet tests for setup/start/init command names.
- Drift test that no-path `awf init` is not described as machine setup.

### T16 - Add Release Workflow Checks For Manifest, Checksums, And Installer Smoke

Owner type: workspace

Modules touched:

- `.github/workflows/publish.yml`
- release scripts
- release docs/tests

Depends on: T11, T12, T13, H01

What:

- Extend release workflow to emit manifest and checksums.
- Add installer smoke that consumes release artifacts or fixture artifacts.
- Keep PyPI/uv/pipx manual install paths first-class.

Acceptance criteria:

- Release workflow fails if manifest or checksums drift.
- Installer smoke verifies downloaded artifact checksum before install.
- Publish docs explain how to verify artifacts manually.

Required tests:

- Workflow/static tests for expected jobs and artifact names.
- Manifest/checksum fixture tests.
- Local script test for release smoke command generation.

### T17 - Add Support-Bundle And Log Redaction Coverage For Setup Secrets

Owner type: workspace

Modules touched:

- `src/awf/service/support_bundle.py`
- `src/awf/common/redaction.py`
- `src/awf/host_setup`
- support-bundle tests

Depends on: T06, T07

What:

- Ensure setup config, provider refs, credential backend metadata, support
  bundles, logs, and doctor output never leak raw provider tokens.
- Redact plain-file secret paths where path disclosure would be sensitive.

Acceptance criteria:

- Support bundles can include useful setup state without raw secret leakage.
- Token-shaped strings are redacted in setup/start logs and diagnostics.
- Plain-file paths are either omitted or redacted according to the approved
  consent model.

Required tests:

- Support-bundle fixture with keyring, env ref, and plain-file metadata.
- Log redaction tests for token-shaped provider values.
- Regression tests that MCP cannot expose raw secret values.

### T18 - Add Docs Drift Tests For Setup/Start/Init Command Grammar

Owner type: workspace

Modules touched:

- `tests/unit/docs`
- public docs
- README snippets

Depends on: T01, T15

What:

- Add docs drift tests that enforce the new command grammar across README,
  quickstart, MCP docs, release docs, and troubleshooting docs.
- Reject legacy text that presents `awf init` no-path as service bootstrap.

Acceptance criteria:

- Docs tests fail on stale `awf init` bootstrap language.
- Docs tests require `awf setup`, `awf start`, and `awf init <repo>` examples
  in the right contexts.
- Docs tests cover the four install lanes.

Required tests:

- Focused docs drift tests under `tests/unit/docs`.
- Link/snippet tests for first-run commands.

### T19 - Final Integration, Full Coverage, And First-Run Lane Validation

Owner type: coordination

Modules touched:

- Cross-cutting validation only unless small fixes are needed.

Depends on: T07, T09, T14, T15, T16, T17, T18

What:

- Merge all completed PRs and run the integrated validation suite.
- Validate first-run lane behavior from outside the source checkout.
- Reconcile docs, reason catalog, MCP parity, release artifacts, and package
  content.

Acceptance criteria:

- Full unit suite passes.
- Coverage is at or above the repo target.
- Installer and source-checkout smoke paths pass.
- `awf setup --dry-run`, `awf start --help`, `awf init --help`,
  `awf mcp serve --help`, and the new MCP setup tools are verified.
- The final PR has no unresolved review comments, CI failures, stale docs, or
  release-artifact drift.

Required validation:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q
python scripts/generate_openapi.py --check
uv run --python 3.12 --extra dev pytest --cov=awf --cov-report=term-missing --cov-fail-under=99
```

## Execution Graph

Legend:

- `Hxx` means human dependency.
- `Txx` means implementation workspace or coordination task.
- `*` means task can run in a workspace.
- `!` means a human dependency is tracked in the graph. In this backlog, H01 and
  H02 are already satisfied by [Locked Human Decisions](#locked-human-decisions).

```text
H01 ! installer hosting/trust (done)
  +--> T11* install manifest
          +--> T12* install.sh
                 +--> T14* E2E first-run lanes
                 +--> T15* docs lanes
          +--> T13* package assets
                 +--> T14* E2E first-run lanes
                 +--> T15* docs lanes

T12* install.sh
  +--> T16* release workflow checks
T13* package assets
  +--> T16* release workflow checks

H02 ! credential consent wording (done)
  +--> T06* credential ref backends
          +--> T07* provider orchestration
                  +--> T17* setup secret redaction

T01* CLI grammar/init switch
  +--> T04* setup dry-run
  +--> T05* start wrapper
  +--> T11* install manifest

T02* config/source asset model
  +--> T13* package assets
  +--> T03* first-run errors/rendering
          +--> T04* setup dry-run
          +--> T05* start wrapper
          +--> T06* credential backends
          +--> T08* client config helpers

T04* setup dry-run
  +--> T07* provider orchestration
  +--> T09* MCP setup tools
  +--> T10* no-token smoke proof
          +--> T14* E2E first-run lanes
          +--> T15* docs lanes
                  +--> T18* docs drift tests

T05* start wrapper
  +--> T09* MCP setup tools
  +--> T10* no-token smoke proof
  +--> T14* E2E first-run lanes

T08* client config helpers
  +--> T09* MCP setup tools

T07* provider orchestration    +--> T19 final integration and coverage
T09* MCP setup tools           +--> T19 final integration and coverage
T14* E2E first-run lanes       +--> T19 final integration and coverage
T15* docs lanes                +--> T19 final integration and coverage
T16* release workflow checks   +--> T19 final integration and coverage
T17* setup secret redaction    +--> T19 final integration and coverage
T18* docs drift tests          +--> T19 final integration and coverage
```

## Eight-Workspace Execution Schedule

This schedule maximizes useful parallelism without launching tasks that are
likely to collide or wait idle.

### Human Gate Before Wave 1

Status: H01 and H02 are already satisfied by
[Locked Human Decisions](#locked-human-decisions). Run this gate again outside
AWF capacity only if a human operator reopens one of those decisions:

| Item | Status | Required before | Notes |
| --- | --- | --- | --- |
| H01 | done - locked | T11, T12, T16 | Installer hosting and trust-chain decision. |
| H02 | done - locked | T06 | Plain-secret consent and provider prompt wording. |

Do not hold T06/T11/T12/T16 for additional H01/H02 approval. If a future
operator reopens H01, skip T11/T12/T16 and fill capacity with only CLI/setup
work. If H02 is reopened, do not launch credential storage.

### Wave 1 - Foundation

Capacity used: 2 workspaces. T11 is intentionally held until the T01 dependency
gate is complete.

| Slot | Task | Why now |
| --- | --- | --- |
| 1 | T01 CLI grammar/init switch | Locks public command names for every downstream task. |
| 2 | T02 config/source asset model | Locks shared setup/start/provider/client contract. |

Recommended launch:

- Launch T01 and T02 first.
- Do not launch T11 in this wave; queue it behind the release manifest gate
  below.

Conflict flags:

- T02 and T11 should not conflict unless both edit package metadata.

### Release Manifest Gate

Capacity used: 1 workspace when T01 is merged or explicitly satisfied. H01 is
already locked.

| Slot | Task | Why now |
| --- | --- | --- |
| 1 | T11 install manifest | H01 is done; can start after T01 is merged or explicitly satisfied by the human operator. |

### Wave 2 - Setup, Start, Credentials, Clients, Packaging

Capacity used: up to 6 workspaces.

Start when T01 and T02 are merged. Launch T03 first because T04/T05/T06/T08
need the shared error contract before they start.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T03 first-run errors/rendering | T02 |
| 2 | T04 setup dry-run | T01, T02, T03 |
| 3 | T05 start wrapper | T01, T02, T03 |
| 4 | T06 credential backends | T02, T03, H02 (done) |
| 5 | T08 client config helpers | T02, T03 |
| 6 | T13 package/source assets | T02, T11 |

Recommended launch:

- Launch T03 first if possible.
- Launch T04/T05/T06/T08 only after T03 is merged or explicitly satisfied by
  the human operator.
- Launch T13 only after T11 is merged or explicitly satisfied by the human
  operator; it can run alongside post-T03 wave-2 work only when both dependency
  gates are clean.

Conflict flags:

- T04, T06, and T08 all touch `src/awf/host_setup`. Keep each task scoped to
  its own module plus shared models only.
- T05 and T13 both care about package/source assets. T13 owns asset inclusion;
  T05 owns startup selection and diagnostics.

### Wave 3 - Integrations And Release Surface

Capacity used: up to 6 workspaces.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T07 provider orchestration | T04, T06 |
| 2 | T09 MCP setup tools | T04, T05, T08 |
| 3 | T10 no-token smoke proof | T04, T05 |
| 4 | T12 install.sh | T11, H01 (done) |
| 5 | T16 release workflow checks | T11, T12, T13, H01 (done) |
| 6 | T17 setup secret redaction | T06, T07 |

Recommended launch:

- Launch T07, T09, T10, and T12 together once their dependencies are merged.
- Launch T16 only after T11, T12, and T13 are merged or explicitly satisfied.
  H01 is already locked. Keep T16 queued while T12 or T13 is still only in PR
  monitor so release checks smoke the final installer and package artifact set.
- Launch T17 after T07 defines provider setup payloads.

Conflict flags:

- T07 and T17 may touch redaction and provider summary structures. Prefer T07
  to define payloads and T17 to harden leakage tests.
- T09 and T08 should not overlap after T08 lands; T09 consumes the client helper
  contract.

### Wave 4 - Documentation And End-To-End Proof

Capacity used: 2 workspaces.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T14 clean-install/source-lane E2E smoke | T05, T10, T12, T13 |
| 2 | T15 README, Quickstart, upgrade, uninstall, and source lanes | T01, T04, T05, T10, T12, T13 |

Recommended launch:

- Run T14 and T15 in parallel after their listed dependencies are merged.
- Keep T15 focused on documenting the implemented setup/start/init/install
  behavior. Avoid changing CLI behavior in this wave.

Conflict flags:

- T14 may expose bugs in earlier tasks. Fixes should usually go back to the
  owning task PR if it is still open; otherwise make a narrow follow-up.
- T15 consumes behavior from T04/T05/T10/T12/T13. Do not launch a docs skeleton
  before those dependencies are merged unless it is split into a separate task.

### Wave 5 - Docs Drift

Capacity used: 1 workspace.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T18 docs drift tests | T01, T15 |

Recommended launch:

- Launch T18 after T15 is merged.
- Keep T18 focused on drift tests and doc corrections. Avoid changing CLI
  behavior in this wave.

### Wave 6 - Final Integration

Capacity used: 1 coordination workspace or local operator run.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T19 final integration and coverage | T07, T09, T14, T15, T16, T17, T18 |

Recommended launch:

- Use one workspace or local operator run for T19.
- Do not run broad final integration in parallel with open implementation PRs
  unless the goal is only early signal. The final answer needs merged code.

## Parallel Lane Summary

```text
Lane A - CLI/setup foundation
  T01 -> T04 -> T07 -> T17 -> T19

Lane B - Config/start/source assets
  T02 -> T03 -> T05 -> T10 -> T14 -> T19
      \       \
       \       +-> T06 -> T07
        +-> T08 -> T09 -> T19

Lane C - Installer/release
  H01(done) -> T11 -> T12 -> T14 -> T19
                     \       \-> T16 -> T19
                      +-> T13 -> T14
                             \-> T16

Lane D - Docs/DX
  T01 -> T04 -> T10 -> T15 -> T18 -> T19
   |      |
   +-> T05 ----^
  H01(done) -> T11 -> T12 -^
                 \-> T13 -^

Lane E - Human credential consent
  H02(done) -> T06
```

At no point does the recommended schedule require more than six simultaneous
workspace tasks. The remaining two slots are intentionally left free for PR
monitor repair, rebases, review-comment fixes, or emergency follow-up work.

## Critical Failure Modes To Preserve In Prompts

Every implementation workspace should include the relevant failure modes in its
prompt:

| Flow | Failure | Required behavior |
| --- | --- | --- |
| installer | manifest checksum mismatch | Abort before install and print reason plus artifact URL. |
| installer | PATH target not reachable | Do not claim success unless `awf` is executable or exact shell fix is printed. |
| setup | Docker missing/offline | Fail readiness, do not start Core. |
| setup | invalid source checkout | Return `SOURCE_CHECKOUT_INVALID` and exact missing marker. |
| setup | keychain unavailable | Offer env refs or explicit plain-file opt-in, no raw secret write by default. |
| setup | provider auth invalid | Mark only that provider unavailable and continue remaining providers. |
| setup | MCP config conflict | Show diff and backup path, require confirmation before write. |
| start | source assets missing/stale | Fail with source asset diagnostic and validation command. |
| start | migration failure | Stop startup and surface Alembic stderr summary. |
| init | no path | Exit code 2 with `awf setup` and `awf start` guidance. |
| MCP | credential exposure | Redact refs and never include raw secret values. |

## Review Notes From `plan-eng-review`

Scope:

- The plan is intentionally too large for one PR. The right execution is staged
  PRs with CLI/config foundation first, then credentials/MCP/installer/docs.

Architecture:

- Use existing service bootstrap, service status, doctor, provider readiness,
  profile onboarding, MCP docs, and release workflow.
- Introduce focused host setup modules instead of growing large existing files.
- Prefer Python `keyring` as the boring credential abstraction.
- Keep installer trust stricter than ordinary Python CLI quickstarts because
  AWF handles source code, Docker, credentials, and PR automation.

Tests:

- Each task must add behavior, edge, and error coverage.
- E2E coverage is required for the install lanes because mocking would hide
  packaging and asset-discovery failures.
- Security tests must prove raw provider tokens cannot leak through CLI, MCP,
  config, logs, support bundles, or docs examples.

Performance:

- Setup dry-run should be bounded and local-first.
- Provider network probes must be bounded and provider-specific.
- Client config detection must read known files only, not scan home dirs.
- Installer hashing should stream from disk or network rather than loading
  large artifacts into memory.
