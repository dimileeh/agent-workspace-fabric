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
- H03 execution model: default to `codex` with `gpt-5.5` and `xhigh`
  reasoning effort for AWF implementation workspaces unless an operator gives
  an explicit per-wave override. Operator override on 2026-06-03: launch T07,
  T08, T10, and T16 with `claude_code`, `claude-opus-4-8`, `high`. Operator
  override on 2026-06-06: launch T18, T20, and T21 with `claude_code`,
  `claude-opus-4-8`, `high`.
- H04 launch preflight: before launching implementation workspaces, clean
  expired AWF resources, rebuild local service images, and rerun AWF bootstrap.

H01 through H04 are complete for scheduling purposes. Later H01-H04 dependency
references keep the graph traceable, but they are not additional human approval
holds unless a human operator explicitly reopens one of the decisions above.

## Verified Progress

Last verified: 2026-06-06 from `git log origin/development`, `gh pr view`,
and local full-coverage results. T09, T18, T20, and T21 are merged into
`development`. T21 implementation work from failed workspace
`ws_8846dc92df1c4f02929f707b` was salvaged into
[PR #428](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/428)
and merged as commit `a071c164` on 2026-06-06T09:15:52Z. T19 was completed as
a local coordination pass with validation recorded in
`plans/T19_FINAL_INTEGRATION_VALIDATION.md`.

| Task | Status | Evidence |
| --- | --- | --- |
| T01 | done - merged | [PR #296](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/296), merge commit `1715777e`, merged 2026-05-29T00:06:11Z |
| T02 | done - merged | [PR #295](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/295), merge commit `72e7ce57`, merged 2026-05-29T01:36:57Z |
| T03 | done - merged | [PR #302](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/302), merge commit `38064c4e`, merged 2026-05-29T22:28:28Z |
| T04 | done - merged | [PR #332](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/332), merge commit `e23f1f96`, merged 2026-05-31T23:23:46Z |
| T05 | done - merged | [PR #319](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/319), merge commit `04ffb8cc`, merged 2026-05-31T01:49:22Z |
| T06 | done - merged | [PR #333](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/333), merge commit `b6a65be`, merged 2026-06-01T01:53:14Z |
| T07 | done - merged | [PR #367](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/367), merge commit `45cb384a`, merged 2026-06-03T18:00:56Z |
| T08 | done - merged | [PR #370](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/370), merge commit `4f44329b`, merged 2026-06-03T22:13:28Z |
| T09 | done - merged | [PR #393](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/393), merge commit `50d02a57`, merged 2026-06-05T23:12:30Z |
| T10 | done - merged | [PR #366](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/366), merge commit `4b81380a`, merged 2026-06-04T00:12:35Z |
| T11 | done - merged | [PR #303](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/303), merge commit `959ac5e9`, merged 2026-05-29T10:09:37Z |
| T12 | done - merged | [PR #318](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/318), merge commit `0cac3fc7`, merged 2026-05-31T07:20:40Z |
| T13 | done - merged | [PR #344](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/344), merge commit `8134826f`, merged 2026-06-01T02:59:00Z |
| T14 | done - merged | [PR #394](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/394), merge commit `f99f2f0b`, merged 2026-06-05T18:25:06Z |
| T15 | done - merged | [PR #390](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/390), merge commit `05c83ac8`, merged 2026-06-05T19:04:39Z |
| T16 | done - merged | [PR #371](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/371), merge commit `f7196bd0`, merged 2026-06-03T18:59:52Z |
| T17 | done - merged | [PR #391](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/391), merge commit `ed9ed32d`, merged 2026-06-05T14:01:49Z |
| T18 | done - merged | [PR #425](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/425), merge commit `ff96f37b`, merged 2026-06-06T03:14:45Z |
| T20 | done - merged | [PR #426](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/426), merge commit `35c7b1bf`, merged 2026-06-05T23:45:33Z |
| T21 | done - merged | [PR #428](https://github.com/dimileeh/aira-agent-workspace-fabric/pull/428), merge commit `a071c164`, merged 2026-06-06T09:15:52Z; source workspace `ws_8846dc92df1c4f02929f707b` |
| T19 | done - validated locally | `plans/T19_FINAL_INTEGRATION_VALIDATION.md`; local final coverage `11656 passed, 1 skipped`, coverage `99.00%` |

Current runnable workspace set:

- None. All backlog tasks are complete.
- No backlog implementation workspace remains active.

Scheduled workspace set:

- None.

Next task:

- None for this backlog.

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
| H03 | Decide AWF implementation workspace execution model | human | done - locked | P0 | - | human |
| H04 | Complete launch preflight for implementation workspaces | human | done - locked | P0 | - | human |
| T01 | Lock public CLI grammar and hard-switch no-path `awf init` | workspace | done - merged (#296) | P0 | H03, H04 | foundation |
| T02 | Add host setup config schema and source-checkout asset model | workspace | done - merged (#295) | P0 | H03, H04 | foundation |
| T03 | Add first-run error contract and rendering helpers | workspace | done - merged (#302) | P0 | T02 | foundation |
| T04 | Add `awf setup --dry-run` system checks and readiness payload | workspace | done - merged (#332) | P0 | T01, T02, T03 | setup |
| T05 | Add `awf start` wrapper over existing service bootstrap | workspace | done - merged (#319) | P0 | T01, T02, T03 | start |
| T06 | Add keychain/env/plain-file credential ref backends | workspace | done - merged (#333) | P0 | T02, T03, H02 | credentials |
| T07 | Add provider setup orchestration with GitHub first-class | workspace | done - merged (#367) | P0 | T04, T06 | credentials |
| T08 | Add Claude/Codex client config diff, backup, and write helpers | workspace | done - merged (#370) | P1 | T02, T03, T04 | clients |
| T09 | Add setup/start/init/client MCP tools | workspace | done - merged (#393) | P1 | T04, T05, T08 | mcp |
| T10 | Add no-token local proof and mocked smoke path | workspace | done - merged (#366) | P0 | T04, T05 | smoke |
| T11 | Add install manifest generator and release metadata contract | workspace | done - merged (#303) | P0 | T01, H01 | release |
| T12 | Add checked-in `install.sh` with checksum verification | workspace | done - merged (#318) | P0 | T11, H01 | installer |
| T13 | Ensure wheel/source packages contain bootstrap and installer assets | workspace | done - merged (#344) | P0 | T02, T11, T12 | packaging |
| T14 | Add clean-install and source-lane E2E smoke harness | workspace | done - merged (#394) | P0 | T05, T10, T12, T13 | e2e |
| T15 | Update README, Quickstart, upgrade, uninstall, and source lanes | workspace | done - merged (#390) | P0 | T01, T04, T05, T10, T12, T13 | docs |
| T16 | Add release workflow checks for manifest, checksums, and installer smoke | workspace | done - merged (#371) | P0 | T11, T12, T13, H01 | release |
| T17 | Add support-bundle and log redaction coverage for setup secrets | workspace | done - merged (#391) | P0 | T06, T07 | security |
| T18 | Add docs drift tests for setup/start/init command grammar | workspace | done - merged (#425) | P1 | T01, T15 | docs |
| T20 | Add explicit uv bootstrap contract to `install.sh` | workspace | done - merged (#426) | P0 | T12, T15 | installer |
| T21 | Add hosted one-line `uninstall.sh` with AWF-managed cleanup contract | workspace | done - merged (#428) | P0 | T12, T15, T20 | installer |
| T19 | Final integration, full coverage, and first-run lane validation | coordination | done - validated locally | P0 | T07, T09, T14, T15, T16, T17, T18, T20, T21 | final |

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

### H03 - Decide AWF Implementation Workspace Execution Model

Owner type: human

Status: done - locked in [Locked Human Decisions](#locked-human-decisions)

Depends on: none

Blocks: T01, T02, and all downstream implementation workspaces

What:

- Approve the agent runner, model, and reasoning effort for this backlog.
- Ensure implementation workspace prompts use the approved execution model.
- Keep execution-model changes out of individual workspace task scopes unless a
  human operator explicitly reopens the decision.

Acceptance criteria:

- The execution model is recorded in
  [Locked Human Decisions](#locked-human-decisions).
- Operators can audit H03 through both the Task Backlog table and the Wave 1
  human gate.
- T01/T02 launch prompts can proceed without additional model-selection
  approval.

### H04 - Complete Launch Preflight For Implementation Workspaces

Owner type: human

Status: done - locked in [Locked Human Decisions](#locked-human-decisions)

Depends on: none

Blocks: T01, T02, and all downstream implementation workspaces

What:

- Clean expired AWF resources before launching implementation workspaces.
- Rebuild local service images that implementation tasks depend on.
- Rerun AWF bootstrap so Wave 1 starts from a current control-plane baseline.

Acceptance criteria:

- The preflight is complete before T01/T02 consume Wave 1 workspace capacity.
- Operators can audit H04 through both the Task Backlog table and the Wave 1
  human gate.
- If H04 becomes stale or is reopened, no implementation workspace launches
  until the preflight is rerun.

### T01 - Lock Public CLI Grammar And Hard-Switch No-Path `awf init`

Owner type: workspace

Status: done - merged in PR #296, merge commit `1715777e`

Modules touched:

- `src/awf/cli`
- `tests/unit/cli`
- docs tests that reference command grammar

Depends on: H03, H04

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

Status: done - merged in PR #295, merge commit `72e7ce57`

Modules touched:

- `src/awf/host_setup`
- `tests/unit/service`
- packaging tests if source asset metadata needs fixtures

Depends on: H03, H04

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

Status: done - merged in PR #302, merge commit `38064c4e`

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

Status: done - merged in PR #332, merge commit `e23f1f96`

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
  `--allow-plain-secrets`, `--source-checkout PATH`, and
  `--format json|pretty`.
- Check Docker, Compose, Git, `gh`, Python/runtime, ports, disk, shell/PATH,
  and local capacity without starting Core.
- Write safe config updates only when not in dry-run mode.

Acceptance criteria:

- `awf setup --dry-run` never writes secrets and never starts Core.
- `awf setup --provider github --dry-run` accepts and forwards the provider
  selector so later provider setup can recheck a single provider.
- `awf setup --allow-plain-secrets` accepts and forwards the plain-file
  consent gate for T06 credential backends without making plain-file storage the
  default.
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
  selectors, unknown provider rejection, and plain-secret consent dispatch.
- Unit tests for pass/fail system-check fixtures.
- Non-interactive secret-needed path returns `INTERACTIVE_INPUT_REQUIRED`.

### T05 - Add `awf start` Wrapper Over Existing Service Bootstrap

Owner type: workspace

Status: done - merged in PR #319, merge commit `04ffb8cc`

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

Status: done - merged in PR #333, merge commit `b6a65be`

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

Status: done - merged in PR #367, merge commit `45cb384a`

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

Status: done - merged in PR #370, merge commit `4f44329b`

Modules touched:

- `src/awf/host_setup/clients.py`
- `src/awf/cli/setup_commands.py` for `--client` parser/dispatch after T04
  lands
- `docs/MCP_SETUP.md`
- client integration tests

Depends on: T02, T03, T04

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

- CLI parser and dispatch tests for `--client claude`, `--client codex`,
  unknown clients, and dry-run no-mutation.
- Config missing, already configured, conflicting config, write failure, backup
  creation, and dry-run no-mutation.
- Official CLI path and file-fallback path with fakes.

### T09 - Add Setup/Start/Init/Client MCP Tools

Owner type: workspace

Status: done - merged in PR #393, merge commit `50d02a57`

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

Status: done - merged in PR #366, merge commit `4b81380a`

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

Status: done - merged in PR #303, merge commit `959ac5e9`

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

Status: done - merged in PR #318, merge commit `0cac3fc7`

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

Status: done - merged in PR #344, merge commit `8134826f`

Modules touched:

- `pyproject.toml`
- package data config
- `src/awf/host_setup`
- package tests

Depends on: T02, T11, T12

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

Status: done - merged in PR #394, merge commit `f99f2f0b`

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

Status: done - merged in PR #390, merge commit `05c83ac8`

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

Status: done - merged in PR #371, merge commit `f7196bd0`

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

Status: done - merged in PR #391, merge commit `ed9ed32d`

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

Status: done - merged in PR #425, merge commit `ff96f37b`

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

### T20 - Add Explicit uv Bootstrap Contract To `install.sh`

Owner type: workspace

Status: done - merged in PR #426, merge commit `35c7b1bf`

Modules touched:

- `packaging/install.sh`
- `tests/unit/installer`
- installer help/quickstart docs only where needed

Depends on: T12, T15

What:

- Make missing-`uv` behavior explicit in the installer contract instead of a
  generic dependency failure.
- Preserve existing behavior when `uv` is already installed.
- Preserve `--method pipx` as the configured fallback without attempting to
  bootstrap `uv`.
- Add an explicit non-interactive opt-in, such as `--bootstrap-uv`, for hosts
  where `uv` is missing.
- In an interactive terminal, allow a confirmed prompt before bootstrapping
  `uv`; declining must leave the machine unchanged and print the manual install
  choices.
- Use the official Astral `uv` installer over HTTPS, with a hermetic test seam
  for fixture/local installer scripts. Do not silently execute a second remote
  installer.
- Make `--dry-run` report the planned bootstrap/install steps without mutating
  the machine.

Acceptance criteria:

- `install.sh --help` documents the `uv` bootstrap contract, including the
  non-interactive flag and `pipx` fallback.
- Missing `uv` in non-interactive mode without explicit opt-in fails with a
  clear reason and actionable commands.
- Missing `uv` with explicit opt-in bootstraps `uv`, then installs AWF through
  the verified wheel path.
- Existing checksum, version/channel, uninstall, PATH advice, and `pipx`
  behaviors remain intact.
- Fixture tests cover prompt accept/decline, dry-run, non-interactive failure,
  explicit opt-in, existing-`uv`, and `pipx` fallback paths without real
  network calls.

Required tests:

- Installer fixture/unit tests under `tests/unit/installer`.
- Shell syntax/help tests for `packaging/install.sh`.
- Narrow docs/help assertions for the new bootstrap contract.

### T21 - Add Hosted One-Line `uninstall.sh` With AWF-Managed Cleanup Contract

Owner type: workspace

Status: done - merged (#428)

Modules touched:

- `packaging/uninstall.sh`
- shared installer/uninstaller shell helpers if T20 introduces them
- `tests/unit/installer`
- release/install docs only where needed

Depends on: T12, T15, T20

What:

- Add a hosted one-line uninstall entrypoint suitable for:
  `curl -fsSL https://aira.pro/awf/uninstall.sh | bash`.
- Reuse or share the existing `install.sh --uninstall` uninstall logic; do not
  create a second divergent uninstall implementation.
- Remove only AWF-managed tool installs and explicitly selected local AWF
  runtime/config/state.
- Preserve user and agent credentials by default, including Claude/Codex auth,
  `gh` config, SSH keys, Git config, provider API keys, and user project repos.
- Add interactive prompts for destructive cleanup choices, including whether to
  remove AWF local runtime state.
- If T20 records that AWF bootstrapped `uv`, offer to uninstall `uv` only when
  that AWF ownership marker is present. Default to preserving `uv`.
- In non-interactive mode, require explicit flags for destructive state cleanup
  and bootstrapped-`uv` removal. Without those flags, preserve credentials,
  project repos, and unrelated tools.
- Keep dry-run/plan output clear enough for security-conscious users to inspect
  exactly what would be removed.

Acceptance criteria:

- `uninstall.sh --help` documents AWF-managed cleanup, credential preservation,
  dry-run behavior, state-removal flags, and optional bootstrapped-`uv` removal.
- One-line uninstall removes an AWF-managed package install without touching
  agent credentials or user project repos.
- Uninstall refuses unmanaged `awf` executables or clearly reports no-op state.
- Optional local state cleanup is interactive by default and explicit in
  non-interactive mode.
- Optional `uv` removal is offered only when AWF installed `uv`; otherwise
  `uv` is preserved.
- Existing `install.sh --uninstall` behavior remains compatible or delegates to
  the shared uninstall implementation.

Required tests:

- Installer/uninstaller fixture tests for managed install removal, unmanaged
  refusal, dry-run, interactive cleanup accept/decline, non-interactive flags,
  credential-preservation guards, and bootstrapped-`uv` ownership-marker logic.
- Shell syntax/help tests for `packaging/uninstall.sh`.
- Narrow docs/help assertions for the hosted uninstall contract.

### T19 - Final Integration, Full Coverage, And First-Run Lane Validation

Owner type: coordination

Status: done - validated locally

Modules touched:

- Cross-cutting validation only unless small fixes are needed.

Depends on: T07, T09, T14, T15, T16, T17, T18, T20, T21

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
- `!` means a human dependency is tracked in the graph. In this backlog, H01
  through H04 are already satisfied by
  [Locked Human Decisions](#locked-human-decisions).

```text
H01 ! installer hosting/trust (done)
  +--> T11* install manifest (done)
          +--> T12* install.sh (done)
                 +--> T13* package assets (done)
                         +--> T14* E2E first-run lanes (done)
                         +--> T15* docs lanes (done)
                                +--> T20* uv bootstrap installer contract (done)
                                      +--> T21* hosted uninstaller contract (done)
                        +--> T16* release workflow checks (done)

H02 ! credential consent wording (done)
  +--> T06* credential ref backends (done)
          +--> T07* provider orchestration (done)
                  +--> T17* setup secret redaction (done)

H03 ! implementation execution model (done)
  +--> T01* CLI grammar/init switch (done)
  +--> T02* config/source asset model (done)

H04 ! launch preflight (done)
  +--> T01* CLI grammar/init switch (done)
  +--> T02* config/source asset model (done)

T01* CLI grammar/init switch (done)
  +--> T04* setup dry-run (done)
  +--> T05* start wrapper (done)
  +--> T11* install manifest (done)

T02* config/source asset model (done)
  +--> T13* package assets (done)
  +--> T03* first-run errors/rendering (done)
          +--> T04* setup dry-run (done)
          +--> T05* start wrapper (done)
          +--> T06* credential backends (done)

T04* setup dry-run (done)
  +--> T07* provider orchestration (done)
  +--> T08* client config helpers (done)
  +--> T09* MCP setup tools (done)
  +--> T10* no-token smoke proof (done)
          +--> T14* E2E first-run lanes (done)
          +--> T15* docs lanes (done)
                  +--> T18* docs drift tests (done)

T05* start wrapper (done)
  +--> T09* MCP setup tools (done)
  +--> T10* no-token smoke proof (done)
  +--> T14* E2E first-run lanes (done)

T08* client config helpers (done)
  +--> T09* MCP setup tools (done)

T07* provider orchestration    +--> T19 final integration and coverage
T09* MCP setup tools           +--> T19 final integration and coverage
T14* E2E first-run lanes       +--> T19 final integration and coverage (done)
T15* docs lanes                +--> T19 final integration and coverage (done)
T16* release workflow checks   +--> T19 final integration and coverage
T17* setup secret redaction    +--> T19 final integration and coverage (done)
T18* docs drift tests          +--> T19 final integration and coverage (done)
T20* uv bootstrap installer    +--> T19 final integration and coverage (done)
T21* hosted uninstaller        +--> T19 final integration and coverage (done)
T19 final integration          (done)
```

## Eight-Workspace Execution Schedule

This schedule maximizes useful parallelism without launching tasks that are
likely to collide or wait idle.

### Current PM Snapshot - 2026-06-06

Verified merged: T01, T02, T03, T04, T05, T06, T07, T08, T09, T10, T11,
T12, T13, T14, T15, T16, T17, T18, T20, and T21.

Scheduled workspaces, using 0 of 8 available slots:

- None.

T19 is complete as a local coordination pass; this backlog has no remaining
runnable tasks.

### Human Gate Before Wave 1

Status: H01 through H04 are already satisfied by
[Locked Human Decisions](#locked-human-decisions). Run this gate again outside
AWF capacity if a human operator reopens one of those decisions or if the H04
preflight becomes stale before Wave 1 launch:

| Item | Status | Required before | Notes |
| --- | --- | --- | --- |
| H01 | done - locked | T11, T12, T13, T16 | Installer hosting and trust-chain decision. |
| H02 | done - locked | T06 | Plain-secret consent and provider prompt wording. |
| H03 | done - locked | T01, T02, downstream implementation workspaces | Default execution model: `codex`, `gpt-5.5`, `xhigh`; T07/T08/T10/T16 use the 2026-06-03 operator override `claude_code`, `claude-opus-4-8`, `high`; T18/T20/T21 use the 2026-06-06 operator override `claude_code`, `claude-opus-4-8`, `high`. |
| H04 | done - locked | T01, T02, downstream implementation workspaces | Launch preflight: clean expired AWF resources, rebuild local service images, rerun AWF bootstrap. |

Do not hold T06/T11/T12/T13/T16 for additional H01/H02 approval. If a future
operator reopens H01, skip T11/T12/T13/T16 and fill capacity with only CLI/setup
work. If H02 is reopened, do not launch credential storage. If H03 is reopened,
pause implementation launches until the workspace execution model is decided.
If H04 is reopened or stale, rerun the launch preflight before starting T01/T02
or any downstream implementation workspace.

### Wave 1 - Foundation

Status: complete. T01 and T02 are merged.

Capacity used: 2 workspaces. T11 was intentionally held until the T01
dependency gate completed.

| Slot | Task | Why now |
| --- | --- | --- |
| 1 | T01 CLI grammar/init switch | Locks public command names for every downstream task. |
| 2 | T02 config/source asset model | Locks shared setup/start/provider/client contract. |

Recommended launch:

- No remaining launch work in this wave.

Conflict flags:

- T02 and T11 should not conflict unless both edit package metadata.

### Release Manifest Gate

Status: complete. T11 is merged.

Capacity used: 1 workspace when T01 is merged or explicitly satisfied. H01 was
already locked.

| Slot | Task | Why now |
| --- | --- | --- |
| 1 | T11 install manifest | H01 is done; can start after T01 is merged or explicitly satisfied by the human operator. |

### Wave 2 - Setup, Start, Credentials, Clients

Current status: complete. T03, T04, T05, T06, and T08 are merged.

Capacity used: up to 5 workspaces over the wave; T08 is queued behind T04.

Start when T01 and T02 are merged. Launch T03 first because T04/T05/T06/T08
need the shared error contract before they start. T08 also waits for T04 so
the setup CLI and dry-run surface exist before client dispatch is added.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T03 first-run errors/rendering | T02 |
| 2 | T04 setup dry-run | T01, T02, T03 |
| 3 | T05 start wrapper | T01, T02, T03 |
| 4 | T06 credential backends | T02, T03, H02 (done) |
| 5 | T08 client config helpers | T02, T03, T04 |

Recommended launch:

- No remaining launch work in this wave.
Conflict flags:

- T04, T06, and T08 all touch `src/awf/host_setup`. Keep each task scoped to
  its own module plus shared models only.
- T04 and T08 share the setup CLI surface. T08 consumes T04's command shell and
  owns only client selector dispatch plus client helper wiring.
### Wave 3 - Integrations And Release Surface

Current status: complete. T07, T08, T09, T10, T12, T13, T16, and T17 are
merged.

Capacity used: up to 7 workspace assignments over the wave; T13 queues behind
T12, and T16 queues behind T13.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T07 provider orchestration | T04, T06 |
| 2 | T09 MCP setup tools | T04, T05, T08 |
| 3 | T10 no-token smoke proof | T04, T05 |
| 4 | T12 install.sh | T11, H01 (done) |
| 5 | T13 package/source assets | T02, T11, T12 |
| 6 | T16 release workflow checks | T11, T12, T13, H01 (done) |
| 7 | T17 setup secret redaction | T06, T07 |

Recommended launch:

- No remaining launch work in this wave.

Conflict flags:

- T07 and T17 may touch redaction and provider summary structures. Prefer T07
  to define payloads and T17 to harden leakage tests.
- T09 and T08 should not overlap after T08 lands; T09 consumes the client helper
  contract.
- T05 and T13 both care about package/source assets. T13 owns asset inclusion;
  T05 owns startup selection and diagnostics.

### Wave 4 - Documentation And End-To-End Proof

Current status: T14 and T15 are merged.

Capacity used: 2 workspaces.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T14 clean-install/source-lane E2E smoke | T05, T10, T12, T13 |
| 2 | T15 README, Quickstart, upgrade, uninstall, and source lanes | T01, T04, T05, T10, T12, T13 |

Recommended launch:

- No remaining launch work in this wave.
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

- No remaining launch work in this wave.
- Keep T18 focused on drift tests and doc corrections. Avoid changing CLI
  behavior in this wave.

### Wave 5B - Installer Bootstrap Follow-Up

Capacity used: 1 workspace at a time.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T20 uv bootstrap installer contract | T12, T15 |
| 1 | T21 hosted one-line uninstaller | T12, T15, T20 |

Recommended launch:

- T21 is merged as PR #428.
- Keep T20 focused on `packaging/install.sh`, installer fixture tests, shell
  help text, and only minimal install-lane docs if needed.
- Keep T21 focused on `packaging/uninstall.sh`, shared uninstall helpers,
  installer/uninstaller fixture tests, and only minimal uninstall-lane docs if
  needed.

### Wave 6 - Final Integration

Status: complete. T19 was executed locally without AWF.

Capacity used: 1 coordination workspace or local operator run.

| Slot | Task | Depends on |
| --- | --- | --- |
| 1 | T19 final integration and coverage | T07, T09, T14, T15, T16, T17, T18, T20, T21 |

Recommended launch:

- No remaining launch work in this wave.
- The final integration run used merged code from `development`.

## Parallel Lane Summary

```text
Lane A - CLI/setup foundation
  T01 -> T04 -> T07 -> T17 -> T19
          \
           +-> T08 -> T09 -> T19

Lane B - Config/start/source assets
  T02 -> T03 -> T05 -> T10 -> T14 -> T19
      \       \
       \       +-> T06 -> T07
        +-------> T08 -> T09 -> T19

Lane C - Installer/release
  H01(done) -> T11 -> T12 -> T13 -> T14 -> T19
                              \
                               +-> T16 -> T19
                               \
                                +-> T20 -> T21 -> T19

Lane D - Docs/DX
  T01 -> T04 -> T10 -> T15 -> T18 -> T19
   |      |
   +-> T05 ----^
  H01(done) -> T11 -> T12 -> T13 -^
                               \
                                +-> T20 -> T21 -> T19

Lane E - Human credential consent
  H02(done) -> T06
```

At no point did the recommended schedule require more than six simultaneous
workspace tasks. This backlog is now complete.

## Critical Failure Modes To Preserve In Prompts

Every implementation workspace should include the relevant failure modes in its
prompt:

| Flow | Failure | Required behavior |
| --- | --- | --- |
| installer | manifest checksum mismatch | Abort before install and print reason plus artifact URL. |
| installer | PATH target not reachable | Do not claim success unless `awf` is executable or exact shell fix is printed. |
| installer | `uv` missing | Do not silently install `uv`; prompt interactively or require explicit non-interactive opt-in, while preserving `pipx` fallback. |
| uninstaller | credential or project deletion risk | Preserve user/agent credentials and project repos by default; remove only AWF-managed install/runtime state after explicit confirmation or flags. |
| uninstaller | `uv` removal | Offer `uv` removal only when an AWF ownership marker proves AWF installed it; default to preserving `uv`. |
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
