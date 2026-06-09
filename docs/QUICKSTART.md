# Quickstart

Pick one lane and follow only that lane. **Lane 0** lets a coding agent install
AWF for you; **Lanes 1–3** are deterministic manual installs. Each lane installs
AWF, runs setup, starts local Core, initializes a project, and runs mocked smoke.

The public curl installer lane is release-gated until the hosted installer URL,
manifest, checksums, and release artifacts are published and verified. Until
then, use one of the lanes below.

## Prerequisites

- Git.
- Docker Desktop or Docker Engine with the Compose plugin running.
- `uv` for the `uv tool` and source lanes, or `pipx` for the `pipx` lane.
- GitHub CLI `gh` if you want AWF to create or monitor PRs.
- At least one coding-agent credential for real workspace execution.

Mocked smoke does not require live GitHub or provider access. Local first-run
service URLs use IPv4 loopback: <http://127.0.0.1:8000> for the API and
<http://127.0.0.1:3000> for the console started by `awf start`.

AWF uses root `.env` for local runtime values. `awf start` seeds it from
`.env.example` when needed; source-checkout operators can also pre-create it
with `cp .env.example .env`.

Use any checked-out project repository, or create a throwaway eval project:

```bash
mkdir -p "$HOME/awf-eval-project"
```

## Lane 0: Let your coding agent install it

AWF orchestrates coding agents, so the fastest path is to have one set it up for
you — it clones the repo, installs AWF, brings up local Core, and onboards your
project, adapting to whatever your machine needs along the way. This is the only
lane that ends with *your* repo onboarded and a green smoke. Paste this into
Claude Code, Codex, or any agent with shell access (replace `<PATH>` with your
project's path):

```text
Set up Agent Workspace Fabric (AWF) on this machine and onboard my repo.
1. Clone https://github.com/dimileeh/agent-workspace-fabric and READ
   skills/awf-scheduler/SKILL.md and docs/QUICKSTART.md before doing anything.
2. Check prerequisites (Docker running, uv, git, and gh authenticated if I want PR
   automation). If any are missing, STOP and tell me — do not guess.
3. Install via the source lane: uv tool install . --force, then awf setup, awf start,
   and awf service status --format pretty.
4. Onboard my project at <PATH>: awf init <PATH> --write-profile --yes, then
   awf smoke run --project <PATH> --mocked-local --format pretty.
5. Stop when the mocked smoke is green and report the profile summary. Do not create
   a real workspace or open a PR unless I ask.
```

The agent reads the bundled `skills/awf-scheduler/SKILL.md` (the operator skill
for driving AWF) so its steps stay grounded in the current commands. Want a
deterministic, reproducible install for CI or scripted setup instead? Use one of
the manual lanes below.

## Lane 1: uv tool or pipx

This lane is release-installed and package-manager mediated. Use it when you
want the published package and do not need to inspect the AWF source checkout.

Install with one package manager:

```bash
uv tool install agent-workspace-fabric
```

```bash
pipx install agent-workspace-fabric
```

Then run first setup from the directory where AWF should keep its local `.env`:

```bash
awf setup
awf start
awf service status --format pretty
awf init "$HOME/awf-eval-project"
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

`awf init "$HOME/awf-eval-project"` is the `awf init <path>` project onboarding
step. If the project already has `.awf/workspace.yml`, AWF validates it; if not,
interactive terminals guide profile creation.

`awf start` starts the local API, worker, database, and web console. Use
`awf start --headless` to skip the console on server or CI hosts, or
`awf start --console-port 3333` to publish the console on another localhost
port.

Upgrade with the same package manager you used to install:

```bash
uv tool upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

```bash
pipx upgrade agent-workspace-fabric
awf start
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

Uninstall with the same package manager:

```bash
uv tool uninstall agent-workspace-fabric
```

```bash
pipx uninstall agent-workspace-fabric
```

## Lane 2: Source Checkout With Global Tool Install

This lane uses inspectable source and installs a global `awf` executable from
that checkout.

```bash
git clone https://github.com/dimileeh/agent-workspace-fabric.git
cd agent-workspace-fabric
uv tool install . --force
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf init "$HOME/awf-eval-project"
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

Upgrade from the same checkout:

```bash
git pull --ff-only
uv tool install . --force
awf setup --source-checkout "$PWD"
awf start --source-checkout "$PWD"
awf service status --format pretty
awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

Uninstall the global executable when you no longer want this lane:

```bash
uv tool uninstall agent-workspace-fabric
```

## Lane 3: Source Checkout With No Global Install

This lane keeps AWF fully inspectable and avoids installing a global executable.
Run AWF through `uv run` from the checkout.

```bash
git clone https://github.com/dimileeh/agent-workspace-fabric.git
cd agent-workspace-fabric
uv sync --extra dev
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf init "$HOME/awf-eval-project"
uv run --python 3.12 --extra dev awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

Upgrade from the same checkout:

```bash
git pull --ff-only
uv sync --extra dev
uv run --python 3.12 --extra dev awf setup --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf start --source-checkout "$PWD"
uv run --python 3.12 --extra dev awf service status --format pretty
uv run --python 3.12 --extra dev awf smoke run --project "$HOME/awf-eval-project" --mocked-local --format pretty
```

There is no global executable to uninstall in this lane. Remove the checkout
only after you no longer need the source tree.

## Raw Docker Compose

For source checkouts or raw Docker installs, root Compose can bring up the full
local stack with safe loopback-only defaults:

```bash
docker compose up -d --build
```

Open <http://127.0.0.1:3000> for the console, or call the API at
<http://127.0.0.1:8000>. Protected local API calls use
`Authorization: Bearer local-dev-token` unless you set `AWF_API_TOKEN`.

## When Something Fails

- Use `awf service doctor` for local prerequisite failures.
- Use `awf service status --format pretty` for local API, database, Docker,
  image, disk, provider, and cleanup health.
- Use `awf service readiness --format pretty` only as the AWF Core
  release-readiness gate; it includes historical PRD SLO evidence and may fail
  even when the local service is healthy.

Next:

- [Project Onboarding](PROJECT_ONBOARDING.md)
- [Upgrade Guide](UPGRADE.md)
- [Uninstall Guide](UNINSTALL.md)
- [PR Monitor Adoption](PR_MONITOR_ADOPTION.md)
- [DX Smoke Command](SMOKE_COMMAND.md)
- [Troubleshooting](TROUBLESHOOTING.md)
