# PR282 CI setup-uv Validation

Plan reference: `plans/PR282_CI_SETUP_UV_PLAN.md`

## Requirement Status

- Add regression coverage for the CI workflow toolchain pin so stale setup-uv
  action refs and wildcard uv versions are caught locally: Complete. Added
  `tests/unit/control/test_ci_workflow_toolchain.py`.
- Replace `astral-sh/setup-uv@v4` in Python CI jobs with a current setup-uv
  action ref that supports non-API-backed version resolution: Complete.
  `.github/workflows/ci.yml` now uses `astral-sh/setup-uv@v8.1.0` in
  `lint-and-type`, `python-full-coverage`, and `release-artifacts`.
- Replace wildcard `version: "0.5.x"` uv pins with a concrete available uv
  release: Complete. The same jobs now use `version: "0.11.15"`.
- Keep the existing CI jobs, coverage command, coverage threshold, Docker
  checks, artifact upload, and required-job fan-in behavior unchanged:
  Complete. The workflow diff only changes setup-uv action refs and uv version
  pins.
- Run focused verification only; AWF/GitHub CI owns broad coverage and full
  workflow validation after agent completion: Complete. Only targeted local
  checks were run.
- Commit the fix locally with a conventional commit message: Complete. This
  validation file is included in the local commit for the fix cycle.

## Evidence

Files changed:

- `.github/workflows/ci.yml`
- `tests/unit/control/test_ci_workflow_toolchain.py`
- `plans/PR282_CI_SETUP_UV_PLAN.md`
- `plans/PR282_CI_SETUP_UV_VALIDATION.md`

Focused commands run:

- `gh run view 26327952299 --repo dimileeh/aira-agent-workspace-fabric --log-failed`
  showed `python-full-coverage` failed inside `astral-sh/setup-uv@v4` with
  `Bad credentials` before pytest started.
- Before the workflow change,
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_ci_workflow_toolchain.py -q`
  failed because setup-uv major version was `4`.
- After the workflow change,
  `uv run --python 3.12 --extra dev pytest tests/unit/control/test_ci_workflow_toolchain.py -q`
  passed with `1 passed`.
- `gh api 'repos/astral-sh/setup-uv/contents/action.yml?ref=v8.1.0' --jq '.content' | base64 -d | rg -n 'version:|using:'`
  passed and showed the `version` input plus `using: "node24"`.
- `gh release view 0.11.15 -R astral-sh/uv --json tagName,assets --jq '{tagName, linux: [.assets[].name | select(test("^uv-x86_64-unknown-linux-gnu"))]}'`
  passed and showed `uv-x86_64-unknown-linux-gnu.tar.gz`.
- `uv run --python 3.12 --extra dev ruff check tests/unit/control/test_ci_workflow_toolchain.py`
  passed after applying the import-order fix.
- `uv run --python 3.12 --extra dev python -c 'import yaml, sys; yaml.safe_load(open(sys.argv[1], encoding="utf-8")); print("yaml ok")' .github/workflows/ci.yml`
  passed.
- `git diff --check` passed.

## Deferred Validation

Full coverage, whole-repository lint/type checks, full frontend builds, Docker
image builds, and CI-equivalent workflow execution were not run locally per the
AWF workspace contract. AWF/GitHub CI owns those broad validation gates after
agent completion.

## Gaps

No implementation gaps remain.
