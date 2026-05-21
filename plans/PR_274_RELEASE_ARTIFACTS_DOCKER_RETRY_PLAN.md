# PR 274 Release Artifacts Docker Retry Plan

## Problem Statement and Scope

PR #274 CI failed in the `release-artifacts` job during `Validate agent-runtime image build`.
The completed GitHub Actions job log shows Docker Buildx failed while resolving
`python:3.12-slim-bookworm` from Docker Hub with HTTP `502 Bad Gateway`.

This is a real CI reliability bug in the release artifact validation path: a transient registry
metadata error can fail the PR even though the validation itself still needs to require successful
Docker image builds.

Scope is limited to the release artifact Docker build validation commands and a focused regression
test for the retry wrapper behavior. The fix must not skip, disable, or weaken the image build
checks.

## Requirements Checklist

- Keep all work on the current AWF-managed branch; do not push or switch branches.
- Preserve the release artifact checks: both Dockerfiles must still be built successfully.
- Add bounded retry behavior for transient Docker build failures while preserving each failed
  attempt's command output and final non-zero exit status when exhausted.
- Keep the retry surface narrow to CI Docker image validation.
- Add a focused regression test before implementation.
- Run focused validation after implementation.
- Commit the fix locally with a conventional `fix(ci): ...` message.

## Implementation Steps

1. Add a failing unit test for a CI Docker build helper that verifies:
   - every failed attempt is logged,
   - retry sleeps happen only between attempts,
   - a later successful attempt returns success,
   - exhausted attempts return the final non-zero exit code.
2. Implement a small Python helper in `scripts/` that runs a command with bounded retries and
   explicit attempt logging.
3. Update `.github/workflows/ci.yml` `release-artifacts` image validation steps to call the helper
   around the existing Buildx command path.
4. Run focused test and lint checks for the changed script/workflow surface.
5. Record validation evidence in `plans/PR_274_RELEASE_ARTIFACTS_DOCKER_RETRY_VALIDATION.md`.
6. Commit the plan, implementation, tests, validation, and workflow change locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_docker_build_retry.py -q`
  - Passes and proves retry behavior.
- `uv run --python 3.12 --extra dev ruff check scripts tests/unit/test_ci_docker_build_retry.py`
  - Passes for changed Python files.
- `uv run --python 3.12 --extra dev ruff format --check scripts tests/unit/test_ci_docker_build_retry.py`
  - Passes for changed Python formatting.
