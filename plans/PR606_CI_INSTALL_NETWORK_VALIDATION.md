# PR #606 CI install-network failure validation

## Plan reference

- `plans/PR606_CI_INSTALL_NETWORK_PLAN.md`

## Requirement-by-requirement status

| # | Requirement | Status | Evidence |
|---|-------------|--------|----------|
| 1 | Inspect PR #606 checks and failing job logs | Complete | `gh pr checks 606` identified failing `python-coverage-shards (1)` and `python-coverage-shards (3)` in CI run `27813927257`; job logs were inspected through the Actions job log API. |
| 2 | Do not edit protected workflow, quality-gate, or configuration files | Complete | No workflow, quality-gate, or configuration files were changed. |
| 3 | Do not weaken, skip, or disable any CI check | Complete | No check behavior or thresholds were changed. |
| 4 | Do not run broad local validation or full coverage gate | Complete | Local verification was limited to the failing executor cleanup test class and ruff on the touched test file. |
| 5 | Avoid product-code changes for transient dependency installation | Complete | The shard 1 `ruff==0.15.17` download failure was documented as transient; no production code was changed for it. |
| 6 | Recheck PR status and rerun failed job only when appropriate | Complete | Status was polled until all shards settled. A targeted rerun of shard 1 was attempted while the workflow was active and GitHub rejected it with `job 82310492551 cannot be rerun`. |
| 7 | Preserve production gitlink safety and fix stale test fixture | Complete | Updated only `_mark_git_worktree` in `tests/unit/control/test_executor_validation_fix_cycle.py` to create a minimal real git repository with a valid `HEAD`. |

## Evidence

### CI failure root causes

- `python-coverage-shards (1)` failed before tests ran during
  `uv sync --python 3.12 --extra dev`.
  - uv failed downloading `ruff==0.15.17` from `files.pythonhosted.org`.
  - Error class: request/connection failure ending in `stream closed because of
    a broken pipe`.
  - Other shards installed the same dependency successfully, so this is a
    transient install/network failure.
- `python-coverage-shards (3)` failed with 8 tests in
  `TestValidationSideEffectCleanup`.
  - The tests created a fake `.git` file pointing at `/tmp/fake.git`.
  - The current validation cleanup code uses real `git ls-tree` via
    `_gitlink_paths()` to avoid deleting deinitialized tracked submodules.
  - Real `git ls-tree` cannot run against the fake marker, so the tests aborted
    with `VALIDATION_WORKTREE_STATUS_FAILED` instead of exercising side-effect
    cleanup.
- `python-coverage-shards (4)` passed after the fix investigation was underway.
- `python-full-coverage` and `ci-required` failed because shard artifacts were
  missing from the failed shards.

### Local verification

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup \
  -q --tb=short
```

Result: `9 passed`.

```bash
uv run --python 3.12 --extra dev ruff check \
  tests/unit/control/test_executor_validation_fix_cycle.py
```

Result: `All checks passed!`.

```bash
uv run --python 3.12 --extra dev ruff format --check \
  tests/unit/control/test_executor_validation_fix_cycle.py
```

Result: `1 file already formatted`.

## Files changed

- `tests/unit/control/test_executor_validation_fix_cycle.py`
  - `_mark_git_worktree` now initializes a minimal real git repository and
    creates an empty initial commit, allowing production gitlink enumeration to
    run in tests without weakening cleanup behavior.
- `plans/PR606_CI_INSTALL_NETWORK_PLAN.md`
  - Saved investigation and implementation plan.
- `plans/PR606_CI_INSTALL_NETWORK_VALIDATION.md`
  - This validation record.

## Remaining gaps

The shard 1 dependency download failure is not a repository-code bug and still
requires a fresh GitHub Actions run after this commit. Full AWF/GitHub
validation remains owned by AWF/GitHub after agent completion; no broad local
coverage gate was executed.
