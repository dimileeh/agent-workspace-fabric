# PR #606 CI install-network failure plan

## Problem statement and scope

PR #606 currently reports failing GitHub Actions checks in CI run
`27813927257`.

The first failure was `python-coverage-shards (1)`. The job did not reach tests
or coverage. Its `Install dependencies` step failed
during:

```bash
uv sync --python 3.12 --extra dev
```

The failing package download was `ruff==0.15.17`, with uv reporting a request
failure to `files.pythonhosted.org` ending in `stream closed because of a broken
pipe`. Other Python coverage shards in the same run completed dependency
installation successfully with the same project environment.

`python-coverage-shards (3)` later surfaced a real test failure in
`tests/unit/control/test_executor_validation_fix_cycle.py`. The
`TestValidationSideEffectCleanup` tests create fake git worktrees by writing a
`.git` control file that points to `/tmp/fake.git`. Recent validation-worktree
cleanup code legitimately calls real git via `_gitlink_paths()` to batch-enumerate
tracked submodule gitlinks. That subprocess cannot work against the fake marker,
so the tests fail with `VALIDATION_WORKTREE_STATUS_FAILED` before exercising the
intended tracked-side-effect cleanup behavior.

Scope is limited to updating the affected unit-test fixture so these executor
tests use a minimal real git repository when they opt into git-aware cleanup.
No production cleanup logic or protected CI/config files should change.

## Explicit requirements checklist

1. Inspect the PR #606 checks and failing job logs through GitHub Actions.
2. Do not edit protected workflow, quality-gate, or configuration files.
3. Do not weaken, skip, or disable any CI check.
4. Do not run broad local validation or the full coverage gate in the agent
   workspace.
5. If the only confirmed failure is transient dependency installation, avoid
   product-code changes and document the evidence.
6. Recheck PR status after the active run settles; rerun only the failed Actions
   job if GitHub allows it and no code failure appears.
7. For the shard 3 test failure, preserve production gitlink safety behavior and
   fix the stale test fixture instead of weakening cleanup.

## Implementation steps

1. Use `gh run list --commit HEAD` and `gh pr checks 606` to identify the
   current failing check.
2. Fetch the failed job log with the Actions job log API because run-level logs
   are unavailable while the workflow is still in progress.
3. Confirm whether the failure occurred before tests/coverage.
4. Poll the remaining in-progress shards for additional failures.
5. Attempt a targeted rerun of the failed job once the workflow state allows it.
6. Reproduce the shard 3 failure locally with the narrow failing test.
7. Update `_mark_git_worktree` in
   `tests/unit/control/test_executor_validation_fix_cycle.py` to initialize a
   minimal real git repository with a valid `HEAD`, so real `git ls-tree` calls
   can run while the async command runner still controls executor git behavior.
8. Re-run the focused side-effect cleanup tests.
9. Record validation evidence in
   `plans/PR606_CI_INSTALL_NETWORK_VALIDATION.md`.

## Verification commands and pass criteria

Focused inspection commands:

```bash
gh run list --commit HEAD --limit 20
gh pr checks 606 --json name,state,bucket,link,startedAt,completedAt,workflow
gh run view 27813927257 --json status,conclusion,jobs
gh api /repos/dimileeh/agent-workspace-fabric/actions/jobs/82310492551/logs
uv run --python 3.12 --extra dev pytest \
  tests/unit/control/test_executor_validation_fix_cycle.py::TestValidationSideEffectCleanup \
  -q --tb=short
```

Pass criteria:

- The failing job log shows no test, coverage, lint, or type failure.
- Other completed shards either pass or expose actionable failures to fix.
- The shard 3 `TestValidationSideEffectCleanup` failures pass locally with a
  focused test command.
- No protected files are edited.
- Full AWF/GitHub validation remains owned by AWF/GitHub after agent completion.
