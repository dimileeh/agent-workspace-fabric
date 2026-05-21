# PR 274 Release Artifacts Docker Retry Validation

Plan reference: `PR_274_RELEASE_ARTIFACTS_DOCKER_RETRY_PLAN.md`

## Requirement Status

- Complete: Kept all work on the current AWF-managed branch; no push or branch switch was
  performed.
- Complete: Preserved release artifact image validation. `.github/workflows/ci.yml` still builds
  both `docker/control-plane.Dockerfile` and `docker/agent-runtime.Dockerfile` for `linux/amd64`
  using Docker Buildx.
- Complete: Added bounded retry behavior through `scripts/ci_docker_build_retry.py`; each attempt
  logs its start, failed attempts log the exit code, command output streams in CI, and exhausted
  retries return the final non-zero exit code.
- Complete: Kept retry surface narrow to the release Docker image validation steps.
- Complete: Added the focused regression test first. Initial red run failed with
  `ImportError: cannot import name 'ci_docker_build_retry' from 'scripts'`.
- Complete: Ran focused validation after implementation.
- Complete: Prepared this validation record before the local commit.

## Evidence

Files changed:

- `.github/workflows/ci.yml`
- `scripts/ci_docker_build_retry.py`
- `tests/unit/test_ci_docker_build_retry.py`
- `plans/PR_274_RELEASE_ARTIFACTS_DOCKER_RETRY_PLAN.md`
- `plans/PR_274_RELEASE_ARTIFACTS_DOCKER_RETRY_VALIDATION.md`

Observed CI failure:

- `release-artifacts` failed in GitHub Actions run `26236979818`, job `77212864453`.
- The failing step was `Validate agent-runtime image build`.
- The job log showed Docker Buildx failed resolving `python:3.12-slim-bookworm` from Docker Hub:
  `502 Bad Gateway`.

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_docker_build_retry.py -q`
  - Initial red run: failed during collection because the helper module did not exist.
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_docker_build_retry.py tests/unit/test_ci_workflow_full_coverage.py -q`
  - Passed: `15 passed in 0.52s`.
- `uv run --python 3.12 --extra dev ruff check scripts tests/unit/test_ci_docker_build_retry.py`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check scripts tests/unit/test_ci_docker_build_retry.py`
  - Passed.
- `uv run --python 3.12 --extra dev python - <<'PY' ... yaml.safe_load(...) ... PY`
  - Passed: `.github/workflows/ci.yml` parsed successfully.
- `uv run --python 3.12 --extra dev python scripts/ci_docker_build_retry.py --attempts 1 -- python -c "print('ci retry helper smoke')"`
  - Passed.

## Validation Limitation

`docker version` showed the Docker client is installed, but the daemon socket is unavailable in this
workspace:

`failed to connect to the docker API at unix:///var/run/docker.sock`

Because of that, the actual image build could not be rerun locally. The next AWF push/CI cycle will
exercise the real release artifact Docker builds with the retry wrapper.
