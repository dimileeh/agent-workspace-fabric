# PRRT_kwDOSJAM6s6Fmwmo Branch Dispatch Manifest Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6Fmwmo` reports that manually dispatched publish
workflow runs on branch refs can produce `awf-install-manifest.json` files whose
artifact URLs point at a release tag that does not exist. The protected workflow
file is not in scope without explicit owned-path approval, so this fix will keep
the workflow unchanged and make the manifest generator skip GitHub Actions branch
refs before writing a manifest.

Scope is limited to `scripts/generate_install_manifest.py`, focused tests in
`tests/unit/scripts/test_generate_install_manifest.py`, and plan/validation docs.

## Requirements Checklist

- Skip manifest generation in GitHub Actions when the current ref is a branch,
  including branch names that start with `v`.
- Remove any pre-existing manifest output when skipping so stale files cannot be
  uploaded.
- Continue generating manifests for local invocations and valid GitHub Actions
  tag refs.
- Preserve existing manifest validation behavior for real release tags.
- Record focused validation evidence only; AWF/GitHub own broad post-agent
  validation.

## Implementation Steps

1. Add a failing regression test for GitHub Actions branch refs producing no
   manifest and deleting stale output.
2. Add a focused tag-ref regression proving release tag generation still works
   under GitHub Actions environment variables.
3. Implement a generator preflight that skips non-tag GitHub Actions refs before
   manifest construction.
4. Re-run the focused manifest tests and targeted lint for changed Python files.
5. Create validation notes with requirement status and focused command evidence.

## Verification Commands And Pass Criteria

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k github_actions_branch`
  - Pass criterion before implementation: fails because branch refs still write
    `awf-install-manifest.json`.
- Green focused tests: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  - Pass criterion after implementation: all manifest-generator tests pass.
- Focused lint: `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  - Pass criterion: no lint findings for changed Python files.

Full AWF/GitHub validation is intentionally not run during this agent phase.
