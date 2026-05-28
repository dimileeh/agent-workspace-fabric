# PR 289 GitHub CLI Docker Build Validation

Plan reference: `plans/PR_289_GH_CLI_DOCKER_BUILD_PLAN.md`

## Requirement Status

- Complete: Keep GitHub CLI version pinned in the agent runtime image.
  `docker/agent-runtime.Dockerfile` still declares `ARG GH_VERSION=2.92.0`.
- Complete: Avoid relying on GitHub's mutable apt package index to retain
  older pinned `gh` versions. The Dockerfile now downloads the versioned
  GitHub CLI release `.deb` asset for the pinned version.
- Complete: Preserve documented amd64 and arm64 image platform support. The
  install block accepts `amd64` and `arm64` from `dpkg --print-architecture`.
- Complete: Update the focused Dockerfile unit contract.
  `tests/unit/test_agent_runtime_dockerfile.py` now asserts the release asset
  installation contract.
- Complete: Run only focused local verification. Full workflow, image-build,
  and coverage validation were not run locally; AWF/GitHub own those broad
  gates after agent completion.
- Complete: Commit the fix locally without pushing or switching branches.
  Local commit is prepared after this validation file.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PR_289_GH_CLI_DOCKER_BUILD_PLAN.md`
- `plans/PR_289_GH_CLI_DOCKER_BUILD_VALIDATION.md`

Focused commands run:

- `gh pr checks 289 --json name,state,bucket,link,startedAt,completedAt,workflow`
  identified failing `python-full-coverage`, `release-artifacts`, and aggregate
  `ci-required`.
- `gh run view 26529242357 --job 78141192983 --log` showed
  `E: Version '2.92.0' for 'gh' was not found` while building the agent runtime.
- `gh run view 26529242357 --job 78141192991 --log` showed the same root cause
  in `release-artifacts`.
- `curl -fsSL https://cli.github.com/packages/dists/stable/main/binary-amd64/Packages`
  showed the mutable apt index currently exposes `gh` `2.93.0`, not `2.92.0`.
- `curl -fsI https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_amd64.deb`
  passed.
- `curl -fsI https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_arm64.deb`
  passed.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passed: `6 passed in 0.42s`.
- `uv run --python 3.12 --extra dev ruff format tests/unit/test_agent_runtime_dockerfile.py`
  reformatted the touched Python test after the local commit hook reported a
  formatting-only failure.
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_agent_runtime_dockerfile.py`
  passed.

## Gaps

No plan requirements remain partial or missing. I intentionally did not run the
CI-equivalent Docker image build or full coverage gate locally because the AWF
workspace contract assigns broad validation provenance to AWF/GitHub after the
agent phase.
