# Console Package Assets Plan

## Summary

Make the current root Compose cold-start work coherent for package/bootstrap
assets by shipping the console build context inside both the wheel and source
distribution. Leave `awf start`, console readiness copy, and broader docs changes
unchanged in this slice.

## Scope

- Add package artifact tests proving the console Docker build inputs are present.
- Update `pyproject.toml` so the wheel ships console assets under
  `awf/bootstrap_assets/apps/console`.
- Update `pyproject.toml` so the sdist ships the same console source/build inputs.
- Avoid packaging generated/local console artifacts such as `.next`,
  `node_modules`, `.env.local`, test reports, and TypeScript build info.

## Non-Goals

- Do not make `awf start` start the console.
- Do not change `awf start` success text or readiness behavior.
- Do not update public docs beyond packaging correctness in this slice.
- Do not publish or open a PR in this slice.

## Test Plan

- First run the focused package content test before the package config change and
  confirm it fails because `apps/console` assets are absent from the wheel/sdist.
- After implementation, run:
  - `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q`
  - `uv build --wheel --out-dir /tmp/awf-wheel-check`
  - Inspect the wheel for `awf/bootstrap_assets/apps/console/Dockerfile`.
- Run `git diff --check development`.
