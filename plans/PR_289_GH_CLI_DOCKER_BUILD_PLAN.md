# PR 289 GitHub CLI Docker Build Plan

## Problem Statement and Scope

PR #289 CI fails in the `python-full-coverage` and `release-artifacts` jobs
while building `docker/agent-runtime.Dockerfile`. Both jobs fail before their
main validation work because the Dockerfile pins `GH_VERSION=2.92.0` and then
asks apt to install `gh=2.92.0` from GitHub's mutable apt repository. The
current package index only exposes `gh` `2.93.0`, so the pinned apt install is
no longer reproducible.

Scope is limited to the agent runtime GitHub CLI installation path, its narrow
unit contract, and this plan/validation evidence. Do not edit GitHub workflow
or broad validation configuration.

## Requirements Checklist

- Keep GitHub CLI version pinned in the agent runtime image.
- Avoid relying on GitHub's mutable apt package index to retain older pinned
  `gh` versions.
- Preserve Dockerfile support for the repo's documented amd64 and arm64 image
  platforms.
- Update the focused Dockerfile unit contract.
- Run only focused local verification; full AWF/GitHub validation remains owned
  by AWF after agent completion.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Change `docker/agent-runtime.Dockerfile` Stage 3 to download the versioned
   GitHub CLI `.deb` release asset for `GH_VERSION` and the current Debian
   architecture.
2. Install the downloaded local `.deb` with apt and keep the existing
   `gh --version` smoke check.
3. Update `tests/unit/test_agent_runtime_dockerfile.py` to assert the release
   asset install contract instead of the apt repository contract.
4. Verify the pinned release asset URL exists for amd64 and arm64.
5. Run the focused unit test for the Dockerfile contract.

## Verification Commands and Pass Criteria

- `curl -fsI https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_amd64.deb`
  passes.
- `curl -fsI https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_arm64.deb`
  passes.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passes.

Full workflow, image-build, and coverage validation are intentionally not run
locally because AWF/GitHub own those broad gates for this workspace.
