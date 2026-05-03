# Start Here: AWF Quickstart

Get a fresh AWF evaluator up and running with a meaningful local proof in under 5 minutes.

## Prerequisites

Before running the commands below, ensure your system has the following installed:
- **git**
- **docker** (Docker Desktop or Docker Engine with the Compose plugin running)
- **uv** (Python package manager)

## Recommended Path

1. **Clone the repository**
   ```bash
   git clone https://github.com/dimileeh/aira-agent-workspace-fabric.git
   cd aira-agent-workspace-fabric
   ```
2. **Install the AWF CLI**
   ```bash
   uv tool install aira-awf
   ```
   *Expected Output Snippet:*
   ```text
   Installed 1 executable: awf
   ```

3. **Bootstrap the local control plane**
   ```bash
   awf init
   ```
   *Expected Output Snippet:*
   ```text
   AWF init: local service bootstrap
     state directory: /home/user/.awf/service
     created: false
     kept existing .env
     bootstrap status: ok

   Next steps:
     - export AWF_GITHUB_TOKEN="$(gh auth token)" so the worker can create PRs.
     - Run `awf service status --format pretty` to verify readiness.
     - Run `awf init <path>` to onboard a project repository.
   ```

## Failure Next-Actions

- **Docker not running:** If `awf init` complains about the Docker daemon, verify that Docker is started and your user has permission to interact with it.
- **Port collisions:** If containers fail to bind, check if you already have services running on port `8000` (API) or `5433` (Postgres) and stop them.
- **Command not found:** If the `awf` command is unrecognized after step 2, ensure `uv`'s tool directory (usually `~/.local/bin`) is in your system `$PATH`.

## Next Steps

Once the control plane is successfully running, you are ready to use AWF on real codebases:
- [Project Onboarding](PROJECT_ONBOARDING.md): Run `awf init <path>` in an existing project to configure AWF for a specific codebase.
- [AWF Setup Documentation](../README.md#setup): Detailed instructions for contributor setup, credential injection, and configuration.
