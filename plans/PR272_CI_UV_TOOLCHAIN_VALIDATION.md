# PR272 CI uv Toolchain Validation

Plan reference: `plans/PR272_CI_UV_TOOLCHAIN_PLAN.md`

## Requirement Status

- Replace the stale floating uv `0.5.x` workflow pin with an available concrete
  uv release: Complete. `.github/workflows/ci.yml` now uses uv `0.11.15` in all
  three Python jobs.
- Use a setup-uv action version whose `version` input supports the selected uv
  release: Complete. `astral-sh/setup-uv@v8.1.0` exposes the `version` input.
- Keep all existing CI validation steps intact: Complete. Only the setup-uv
  action tag and uv version changed.
- Verify the selected upstream action tag and uv release asset exist: Complete.
  The setup-uv tag was read from GitHub, and uv `0.11.15` includes
  `uv-x86_64-unknown-linux-gnu.tar.gz`.
- Run focused local validation for the edited workflow where practical:
  Complete. YAML parsing and diff checks passed; ruff format was attempted and
  documented as not applicable to GitHub workflow YAML.
- Commit the fix locally with a conventional commit message: Complete. This
  validation file is included in the local conventional commit required by AWF.

## Evidence

Files changed:

- `.github/workflows/ci.yml`
- `plans/PR272_CI_UV_TOOLCHAIN_PLAN.md`
- `plans/PR272_CI_UV_TOOLCHAIN_VALIDATION.md`

Commands run:

- `GH_CONFIG_DIR=/tmp/awf-gh-config gh run view 26180545736 --log-failed`
  showed the failed jobs were caused by `0.5.x` setup-uv resolution/download
  failures and an old uv-managed Python artifact URL returning 404.
- `GH_CONFIG_DIR=/tmp/awf-gh-config gh api repos/astral-sh/setup-uv/contents/action.yml?ref=v8.1.0 --jq '.content' | base64 -d | rg -n 'version:|using:|node24'`
  passed and showed the selected action tag exposes `version`.
- `GH_CONFIG_DIR=/tmp/awf-gh-config gh release view 0.11.15 -R astral-sh/uv --json tagName,createdAt,assets --jq '{tagName, createdAt, linux: [.assets[].name | select(test("^uv-x86_64-unknown-linux-gnu"))]}'`
  passed and showed the Linux uv artifact exists.
- `rg "setup-uv|version: \"0\\.11\\.15\"|0\\.5\\.x" -n .github/workflows/ci.yml`
  passed and showed all workflow uv pins were updated with no remaining
  `0.5.x` reference.
- `git diff --check` passed.
- `uv run --python 3.12 --extra dev ruff format --check .github/workflows/ci.yml`
  failed because ruff parses the YAML file as Python (`Expected an expression`);
  this is not an applicable YAML validation command.
- `uv run --python 3.12 --extra dev python -c 'import yaml, sys; yaml.safe_load(open(sys.argv[1], encoding="utf-8")); print("yaml ok")' .github/workflows/ci.yml`
  passed.

## Iteration Notes

No implementation gaps remain after the workflow pin update.
