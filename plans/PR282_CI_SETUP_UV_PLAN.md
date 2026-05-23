# PR282 CI setup-uv Plan

## Problem Statement and Scope

PR #282 fails in the required `python-full-coverage` GitHub Actions job before
pytest starts. The failed run reports `Bad credentials` from
`astral-sh/setup-uv@v4` while resolving the workflow's wildcard uv version
`0.5.x`.

Scope is limited to making the Python CI toolchain setup deterministic and less
dependent on GitHub API credential resolution. The coverage command, threshold,
job requirements, and validation steps must stay intact.

## Requirements Checklist

- Add regression coverage for the CI workflow toolchain pin so stale setup-uv
  action refs and wildcard uv versions are caught locally.
- Replace `astral-sh/setup-uv@v4` in Python CI jobs with a current setup-uv
  action ref that supports non-API-backed version resolution.
- Replace wildcard `version: "0.5.x"` uv pins with a concrete available uv
  release.
- Keep the existing CI jobs, coverage command, coverage threshold, Docker
  checks, artifact upload, and required-job fan-in behavior unchanged.
- Run focused verification only; AWF/GitHub CI owns broad coverage and full
  workflow validation after agent completion.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Add a focused unit test that parses `.github/workflows/ci.yml` and asserts
   Python jobs use setup-uv major version 8 or newer with a concrete uv version.
2. Run that single test to confirm it fails on the current workflow.
3. Update `.github/workflows/ci.yml` setup-uv steps in Python jobs to use
   `astral-sh/setup-uv@v8.1.0` and uv `0.11.15`.
4. Verify upstream metadata for the selected setup-uv tag and uv release asset.
5. Run focused checks for the new regression test, touched Python test file,
   workflow YAML parsing, and whitespace.
6. Record validation evidence in `plans/PR282_CI_SETUP_UV_VALIDATION.md`.
7. Commit the workflow, regression test, plan, and validation notes locally.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_ci_workflow_toolchain.py -q`
  fails before the workflow change and passes after it.
- `GH_CONFIG_DIR=/tmp/awf-gh-config gh api repos/astral-sh/setup-uv/contents/action.yml?ref=v8.1.0`
  shows the action exposes the `version` input.
- `GH_CONFIG_DIR=/tmp/awf-gh-config gh release view 0.11.15 -R astral-sh/uv`
  shows a Linux uv release asset exists.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_ci_workflow_toolchain.py`
  passes.
- `uv run --python 3.12 --extra dev python -c 'import yaml, sys; yaml.safe_load(open(sys.argv[1], encoding="utf-8")); print("yaml ok")' .github/workflows/ci.yml`
  passes.
- `git diff --check` passes.

Full coverage, whole-repository lint/type checks, full frontend builds, and
CI-equivalent workflow execution are intentionally left to AWF/GitHub after the
agent phase.
