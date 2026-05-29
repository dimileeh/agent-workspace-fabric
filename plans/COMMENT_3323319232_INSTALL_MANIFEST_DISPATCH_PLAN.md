# Comment 3323319232 Install Manifest Dispatch Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Fooe0` reports that
`scripts/generate_install_manifest.py` skips manifest generation for
GitHub Actions branch refs. The publish workflow's manual
`workflow_dispatch` path runs from a branch when maintainers publish to
TestPyPI/PyPI, so the generator exits successfully before writing
`artifacts/release/awf-install-manifest.json`.

Scope is limited to the manifest generator and its focused unit tests. Do not
edit protected GitHub workflow files.

## Requirements Checklist

- Manual GitHub Actions `workflow_dispatch` runs from a branch generate the
  manifest instead of returning `SKIP`.
- Non-dispatch GitHub Actions branch refs continue to skip to avoid producing
  unproven release manifests during ordinary branch CI usage.
- Existing tag-ref behavior remains unchanged.
- Stale output removal remains covered for skipped refs.
- Verification uses focused tests and focused lint only; broad AWF/GitHub
  validation remains owned by AWF after agent completion.

## Implementation Steps

1. Update the generator test that currently expects branch dispatch skipping
   so it instead covers non-dispatch branch refs.
2. Add a regression test for `GITHUB_EVENT_NAME=workflow_dispatch` with
   `GITHUB_REF_TYPE=branch` proving the manifest is written.
3. Update `_github_actions_skip_reason` to allow manual workflow dispatch
   branch refs while preserving tag and skip behavior for other refs.
4. Run the new regression first to confirm failure, then implement and rerun
   focused tests.

## Verification Commands And Pass Criteria

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k "workflow_dispatch or branch_ref"` fails before the implementation change.
- Green check: the same command passes after implementation.
- Focused lint: `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py` passes.

Do not run full unit suites, full coverage gates, frontend builds, OpenAPI
drift checks, Docker builds, or CI-equivalent validation during this agent
phase.
