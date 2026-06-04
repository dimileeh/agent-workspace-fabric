# Console Package Assets Validation

## Summary

The package/bootstrap asset path now includes the console Docker build context in
both the AWF wheel and source distribution. The implementation leaves `awf start`,
console readiness messaging, and public docs behavior unchanged.

## Red Test Evidence

Before updating `pyproject.toml`, the new focused package tests failed because
the distributions did not contain `apps/console` build inputs:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q -k 'console_build_asset or generated_console_artifacts'
```

Result:

- `22 failed, 2 passed, 29 deselected`
- Failures covered missing console assets in both wheel and sdist.

## Implementation Evidence

- Added console source/config asset assertions to
  `tests/unit/cli/test_package_build_contents.py`.
- Added explicit wheel `force-include` entries for console build inputs under
  `awf/bootstrap_assets/apps/console`.
- Added sdist include entries for the same console build inputs.
- Added sdist `force-include` for `apps/console/lib` because the wheel is built
  from the generated sdist and the normal include traversal did not carry that
  directory.
- Kept generated/local artifacts excluded:
  - `.env.local`
  - `.next`
  - `node_modules`
  - `playwright-report`
  - `test-results`
  - `tsconfig.tsbuildinfo`

## Green Validation

```bash
rm -rf /tmp/awf-package-debug && uv build --out-dir /tmp/awf-package-debug
```

Result:

- Source distribution built.
- Wheel built from source distribution.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q -k 'console_build_asset or generated_console_artifacts'
```

Result:

- `24 passed, 29 deselected`

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q
```

Result:

- `53 passed`

```bash
git diff --check development
```

Result:

- Passed with no whitespace errors.

Wheel inspection confirmed representative console assets are present:

- `awf/bootstrap_assets/apps/console/Dockerfile`
- `awf/bootstrap_assets/apps/console/package.json`
- `awf/bootstrap_assets/apps/console/package-lock.json`
- `awf/bootstrap_assets/apps/console/app/page.tsx`
- `awf/bootstrap_assets/apps/console/components/console-dashboard.tsx`
- `awf/bootstrap_assets/apps/console/lib/awf-server.ts`
- `awf/bootstrap_assets/apps/console/scripts/prepare-playwright-ci-bin.mjs`

## Remaining Scope

- `awf start` still does not start the console in this slice.
- Console readiness/status copy remains unchanged in this slice.
- Public docs updates remain deferred until the package path is ready for the
  broader first-run/docs follow-up.
