# PR272 CI uv Toolchain Plan

## Problem Statement and Scope

PR #272 has failing GitHub Actions jobs because the Python CI workflow installs
uv using the obsolete floating range `0.5.x`. The failed run showed setup-uv
either could not resolve that range or resolved it to artifacts that now return
404s, which caused `lint-and-type`, `python-full-coverage`, and
`release-artifacts` to fail before project validation could run.

Scope is limited to repairing the CI toolchain pin without disabling,
skipping, or weakening any check.

## Requirements Checklist

- Replace the stale floating uv `0.5.x` workflow pin with an available concrete
  uv release.
- Use a setup-uv action version whose `version` input supports the selected uv
  release.
- Keep all existing CI validation steps intact.
- Verify the selected upstream action tag and uv release asset exist.
- Run focused local validation for the edited workflow where practical.
- Commit the fix locally with a conventional commit message.

## Implementation Steps

1. Inspect failed GitHub Actions logs for the exact failing step and artifact
   URL.
2. Check current upstream setup-uv tags and uv release assets.
3. Update `.github/workflows/ci.yml` to use a concrete, available uv release
   and a current setup-uv action tag in every Python job.
4. Run focused validation against the workflow file and selected upstream
   versions.
5. Record validation results in `plans/PR272_CI_UV_TOOLCHAIN_VALIDATION.md`.
6. Commit the workflow fix and planning/validation notes locally.

## Verification Commands and Pass Criteria

- `GH_CONFIG_DIR=/tmp/awf-gh-config gh api repos/astral-sh/setup-uv/contents/action.yml?ref=<tag>`
  must show the action exposes the `version` input.
- `GH_CONFIG_DIR=/tmp/awf-gh-config gh release view <uv-version> -R astral-sh/uv`
  must show the selected Linux uv asset exists.
- `uv run --python 3.12 --extra dev ruff format --check .github/workflows/ci.yml`
  should pass or be documented if the formatter does not support workflow YAML.
- `git diff --check` must pass.
