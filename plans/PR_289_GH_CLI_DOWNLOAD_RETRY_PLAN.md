# PR 289 GitHub CLI Download Retry Plan

## Problem Statement and Scope

PR #289 currently fails the `python-full-coverage` GitHub Actions check while
building `docker/agent-runtime.Dockerfile`. The failing step downloads the
pinned GitHub CLI release `.deb` from GitHub and exits on `curl: (35) Recv
failure: Connection reset by peer` before checksum verification or test
execution can start.

Scope is limited to making the pinned GitHub CLI release asset download
resilient to transient transport failures while preserving the existing checksum
verification, architecture guards, and local `.deb` install behavior. Do not
edit workflows or weaken the CI check.

## Requirements Checklist

- Keep `GH_VERSION` pinned and keep the embedded per-architecture SHA256
  verification.
- Retry only the release asset transport operation; checksum mismatch,
  unsupported architecture, and install failures must remain hard failures.
- Preserve useful `curl` error output and final exit status if all attempts
  fail.
- Add or update a focused regression test for the Dockerfile contract.
- Run only focused local verification. Full AWF/GitHub validation remains owned
  by AWF after agent completion.
- Commit the fix locally without pushing or switching branches.

## Implementation Steps

1. Update `tests/unit/test_agent_runtime_dockerfile.py` to require retry-capable
   `curl` flags on the GitHub CLI release `.deb` download.
2. Run that focused test and confirm it fails against the current Dockerfile.
3. Update `docker/agent-runtime.Dockerfile` to use `curl` retry options for the
   GitHub CLI release asset download while keeping the subsequent SHA256 check
   unchanged.
4. Re-run the focused Dockerfile test.
5. Capture the validation evidence in
   `plans/PR_289_GH_CLI_DOWNLOAD_RETRY_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  fails after the test update and before the Dockerfile fix.
- The same focused pytest command passes after the Dockerfile fix.
- `git diff --check` passes for the touched files.

Full workflow, Docker image build, and coverage validation are intentionally not
run locally because AWF/GitHub own those broad gates for this workspace.
