# PR403 Parallel Coverage CI Plan

## Problem Statement

PR #403's GitHub Actions `python-full-coverage` job was cancelled after its
60-minute timeout. The job currently runs the entire Python test suite and
coverage gate inside one runner, so the required `ci-required` rollup fails even
when lint, console, and release-artifact checks pass.

## Scope

- Replace the single long-running coverage pytest job with parallel GitHub
  Actions coverage shards, following the precedent in
  `../aira/aira-agent/.github/workflows/backend-ci.yml`.
- Preserve the public `python-full-coverage` check as the aggregate coverage
  gate so branch protection and PR monitor logic do not need a rename.
- Keep `ci-required` as the branch-protection rollup and keep it dependent on
  `python-full-coverage`, `lint-and-type`, `console`, and `release-artifacts`.
- Add `pytest-split` to the dev toolchain for stable shard selection.
- Update contributor/agent docs that describe the coverage gate.

## Implementation Steps

1. Update workflow regression tests to require:
   - a `python-coverage-shards` matrix job with eight shards,
   - shard coverage execution via `coverage run --parallel-mode -m pytest`,
   - per-shard coverage artifact uploads,
   - an aggregate `python-full-coverage` job that downloads shards, combines
     coverage, emits `coverage.xml`, and enforces the exact 99% threshold,
   - `ci-required` depending on the aggregate job only.
2. Update `.github/workflows/ci.yml` to add the sharded coverage matrix and
   aggregate coverage gate.
3. Add `pytest-split` to the `dev` extra and refresh `uv.lock`.
4. Update docs that describe the CI coverage job.
5. Validate with workflow tests, dependency lock checks, and lint/format on the
   edited test file.

## Verification Commands

- `uv lock --check`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py -q`
- `uv run --python 3.12 --extra dev ruff check tests/unit/test_ci_workflow_full_coverage.py`
- `uv run --python 3.12 --extra dev ruff format --check tests/unit/test_ci_workflow_full_coverage.py`
- `git diff --check`

## Pass Criteria

- Workflow tests fail before the workflow changes and pass after them.
- The workflow exposes eight parallel coverage shards and one aggregate
  `python-full-coverage` gate.
- `ci-required` continues to depend on `python-full-coverage`, not every matrix
  child.
- The lockfile contains `pytest-split` through the `dev` extra.
