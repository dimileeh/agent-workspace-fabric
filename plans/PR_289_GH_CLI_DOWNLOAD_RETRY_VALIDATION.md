# PR 289 GitHub CLI Download Retry Validation

Plan reference: `plans/PR_289_GH_CLI_DOWNLOAD_RETRY_PLAN.md`

## Requirement Status

- Complete: Keep `GH_VERSION` pinned and keep embedded per-architecture SHA256
  verification. The Dockerfile still pins `GH_VERSION=2.92.0` and verifies the
  downloaded `.deb` against the amd64/arm64 hashes before installation.
- Complete: Retry only the release asset transport operation. The retry flags
  apply to the GitHub CLI `.deb` download command; unsupported architecture,
  missing hash, checksum mismatch, and `apt-get install` remain hard failures.
- Complete: Preserve useful `curl` error output and final exit status if all
  attempts fail. The command uses `--fail --show-error --location --silent` plus
  bounded retry options, so repeated failures still exit from `curl`.
- Complete: Add focused regression coverage. The Dockerfile unit test now
  asserts the retry-capable GitHub CLI release download contract.
- Complete: Run only focused local verification. Full workflow, Docker image
  build, and coverage validation were not run locally; AWF/GitHub own those
  broad gates after agent completion.
- Complete: Commit the fix locally without pushing or switching branches. The
  local commit is prepared after this validation file.

## Evidence

Files changed:

- `docker/agent-runtime.Dockerfile`
- `tests/unit/test_agent_runtime_dockerfile.py`
- `plans/PR_289_GH_CLI_DOWNLOAD_RETRY_PLAN.md`
- `plans/PR_289_GH_CLI_DOWNLOAD_RETRY_VALIDATION.md`

Focused commands run:

- `env GH_CONFIG_DIR=/tmp/awf-gh-clean gh pr checks 289 --repo dimileeh/aira-agent-workspace-fabric --json name,state,bucket,link,startedAt,completedAt,workflow`
  identified failed `python-full-coverage` and aggregate `ci-required`; the
  later refresh showed `release-artifacts` passed.
- `env GH_CONFIG_DIR=/tmp/awf-gh-clean python /home/agent/.codex/plugins/cache/openai-curated/github/1ac32d41/skills/gh-fix-ci/scripts/inspect_pr_checks.py --repo . --pr 289 --json`
  captured the failing job log: `curl: (35) Recv failure: Connection reset by
  peer` while downloading
  `https://github.com/cli/cli/releases/download/v2.92.0/gh_2.92.0_linux_amd64.deb`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  failed before the Dockerfile fix: `1 failed, 5 passed`.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  passed after the Dockerfile fix: `6 passed in 0.37s`.
- `git diff --check` passed.

## Gaps

No plan requirements remain partial or missing. I intentionally did not run the
CI-equivalent Docker image build or full coverage gate locally because the AWF
workspace contract assigns broad validation provenance to AWF/GitHub after the
agent phase.
