# PR 289 Review Comment 4374890254 SHA256 Plan

## Problem Statement and Scope

CodeRabbit review comment `4374890254` asks for integrity verification before
installing the GitHub CLI release `.deb` in `docker/agent-runtime.Dockerfile`.
The current Dockerfile already downloads the upstream checksum file, but its
`grep | sha256sum -c -` verification can silently pass if the asset entry is
missing because the Dockerfile uses `/bin/sh` without `pipefail`.

Scope is limited to the GitHub CLI install block, the focused Dockerfile
contract test, and validation evidence for this review comment.

## Requirements Checklist

- Keep downloading the pinned `GH_VERSION` release asset and matching checksum
  file atomically with the existing `gh_deb` and `GH_VERSION` variables.
- Compute and compare the local `.deb` SHA256 against the expected checksum.
- Abort before `apt-get install` when the checksum entry is missing or the hash
  differs.
- Keep trap-based cleanup of temporary GitHub CLI files.
- Keep `gh --version` at the end of the install block.
- Run only focused local verification; full AWF/GitHub validation remains owned
  by AWF after agent completion.
- Commit the review fix locally without pushing or switching branches.

## Implementation Steps

1. Update `tests/unit/test_agent_runtime_dockerfile.py` to require explicit
   expected and actual checksum variables, an explicit mismatch branch, and no
   pipeline-based `grep -F ... | sha256sum -c -` check.
2. Run the focused Dockerfile unit test and confirm the new assertion fails
   against the current Dockerfile.
3. Update `docker/agent-runtime.Dockerfile` Stage 3 to extract the expected
   hash from the checksum file, compute the local hash, compare them, and exit
   nonzero before install if they differ.
4. Re-run the focused unit test.
5. Record validation evidence in
   `plans/PR_289_REVIEW_COMMENT_4374890254_SHA256_VALIDATION.md`.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_agent_runtime_dockerfile.py -q`
  fails after the test-only change because the Dockerfile still uses the
  pipeline-based checksum verification.
- The same focused test command passes after the Dockerfile update.

Full workflow, image-build, and coverage validation are intentionally not run
locally because AWF/GitHub own those broad gates after agent completion.
