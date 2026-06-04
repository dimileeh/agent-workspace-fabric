# PR403 Parallel Coverage CI Validation

Plan: `plans/PR403_PARALLEL_COVERAGE_CI_PLAN.md`

## Requirement Status

- Replace the single long-running coverage pytest job with parallel shards:
  Complete. `.github/workflows/ci.yml` now has an 8-way
  `python-coverage-shards` matrix.
- Preserve the public aggregate `python-full-coverage` check:
  Complete. `python-full-coverage` now depends on the shard matrix, downloads
  shard artifacts, combines coverage, emits `coverage.xml`, and enforces the
  exact 99% threshold.
- Keep `ci-required` dependent on the aggregate coverage status:
  Complete. `ci-required` still needs `python-full-coverage`, not every matrix
  child.
- Add `pytest-split` to the dev toolchain:
  Complete. `pyproject.toml` and `uv.lock` include `pytest-split>=0.10.0`.
- Update docs:
  Complete. `CONTRIBUTING.md` and `CLAUDE.md` now describe the shard/aggregate
  coverage flow.

## Evidence

- GitHub failure evidence:
  - PR #403 run `26970236806` showed `python-full-coverage` cancelled at the
    60-minute timeout, causing `ci-required` to fail.
- Initial red workflow tests:
  - `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q`
  - Result before implementation: 5 failures for missing `python-coverage-shards`,
    old single-job `python-full-coverage`, and stale docs.
- Final validation commands:
  - `uv lock --check`
  - `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q`
    - Result: 17 passed.
  - `uv run --python 3.12 --extra dev ruff check tests/unit/test_ci_workflow_full_coverage.py`
  - `uv run --python 3.12 --extra dev ruff format --check tests/unit/test_ci_workflow_full_coverage.py`
  - `git diff --check`

## Residual Notes

- Full GitHub CI validation is left to the PR branch after push. The local
  validation proves the workflow contract and dependency lock shape, but does
  not run the full sharded coverage suite locally.
- The coverage shard job intentionally does not eagerly build
  `awf-agent-runtime:latest` in every shard. The integration test that needs
  the image already builds it on demand under `CI=true`, while
  `release-artifacts` continues to validate the agent-runtime Dockerfile.
