# CI wrapt install validation

Plan reference: `plans/CI_WRAPT_INSTALL_PLAN.md`

## Requirement status

- Complete: Preserve AWF branch ownership. No branch switch, push, rebase, or
  broad validation was run.
- Complete: Do not touch protected workflow or quality-gate files. The workflow
  remains unchanged.
- Complete: Make the lint/type job's unlocked dev install avoid resolving a
  newer unverified `wrapt`. `pyproject.toml` now constrains the dev extra to
  `wrapt>=2.1.2,<2.2.0`, matching the locked package line.
- Complete: Keep `uv.lock` consistent with `pyproject.toml`. `uv.lock` records
  `wrapt` as a direct dev extra requirement and keeps version `2.1.2`.
- Complete: Run focused dependency verification only.
- Complete: Record validation evidence in this document.
- Complete: Commit the focused fix locally with a conventional commit message.

## Evidence

Files changed:

- `pyproject.toml`
- `uv.lock`
- `plans/CI_WRAPT_INSTALL_PLAN.md`
- `plans/CI_WRAPT_INSTALL_VALIDATION.md`

Commands run:

- `gh pr view 614 --json number,url,title,headRefName,headRefOid,baseRefName,state,mergeStateStatus,statusCheckRollup,files`
  - Evidence: `lint-and-type` failed; console and release-artifacts later
    passed; coverage shards were still in progress.
- `gh api /repos/dimileeh/agent-workspace-fabric/actions/jobs/82317581285/logs`
  - Evidence: `uv pip install -e ".[dev]"` failed fetching
    `wrapt-2.2.1` metadata from PyPI with a broken pipe before lint/type
    execution.
- `uv lock`
  - Result: passed; resolved 106 packages without version churn.
- `uv lock --check`
  - Result: passed.
- `uv pip install -e ".[dev]"`
  - Result: passed.
- `uv pip show wrapt`
  - Result: `Version: 2.1.2`, required by `testcontainers`.

Full AWF/GitHub validation was not run locally per the workspace contract; AWF
owns the broad post-agent validation and merge-gating run.
