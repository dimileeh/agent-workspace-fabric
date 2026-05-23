# PR #281 CI Check Failure Fix Plan

## Problem Statement

The current CI run for PR `#281` shows failures in:

- `lint-and-type`
- `release-artifacts`

Both fail during `Install uv` with the check-run annotation:

- `Bad credentials - https://docs.github.com/rest`

This blocks `CI` regardless of actual Python lint/type/package code and should be treated as a workflow-level regression.

## Scope

- **In scope**
  - Update workflow bootstrap for `uv` in the failing jobs.
  - Preserve all lint/type/build behavior after uv is available.
  - Keep changes minimal and isolated to CI orchestration.

- **Out of scope**
  - Refactors to runtime code under `src/` or production behavior.
  - Test suite expansion beyond workflow validation.

## Requirements

1. Replace the failing `setup-uv` bootstrap path in each affected job with a method that does not rely on GitHub token-based release lookup in `astral-sh/setup-uv@v4`.
2. Keep uv installation and usage semantics unchanged (`uv python install`, `uv venv`, `uv pip/install`, `python -m build`).
3. Avoid changing unrelated jobs (`python-full-coverage`, `console`) or adding broad CI policy changes.

## Implementation Plan

1. Update `.github/workflows/ci.yml` in:
   - `lint-and-type`
   - `release-artifacts`
2. Replace each `Install uv` step to use `astral-sh/setup-uv@v8` pinned to a specific commit ref with `version: "0.5.x"` and explicit `github-token: ${{ github.token }}` to avoid transient authentication edge cases in older action behavior.
3. Keep all downstream steps unchanged.

## Verification

- Run a focused command to confirm the workflow file contains only the intended uv step changes.
- Re-run CI for this branch (or wait for the next automated rerun) and confirm `Install uv` succeeds in both jobs, then `ci-required` passes once existing test checks are green.

## Pass Criteria

- `lint-and-type` and `release-artifacts` complete `Install uv` successfully.
- No functional change to Python command behavior in subsequent steps.
- No unrelated files changed.
