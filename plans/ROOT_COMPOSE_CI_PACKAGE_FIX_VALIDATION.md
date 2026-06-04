# Root Compose CI Package Fix Validation

## CI Failure

PR #403 failed immediately in GitHub Actions:

- `lint-and-type`
- `python-full-coverage`
- `release-artifacts`

All three failures shared the same root cause: package installation/build tried
to force-include `apps/console/next-env.d.ts`, but that file is ignored by
`apps/console/.gitignore` and absent in a clean GitHub Actions checkout.

Representative release-artifacts error:

```text
FileNotFoundError: Forced include not found:
.../apps/console/next-env.d.ts
```

## Red Test Evidence

Added a regression proving console package inputs must be tracked sources:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q -k tracked
```

Before the fix:

- failed on `apps/console/next-env.d.ts`

## Fix

- Removed `apps/console/next-env.d.ts` from wheel `force-include`.
- Removed `/apps/console/next-env.d.ts` from sdist `include`.
- Added it to generated/local console artifact exclusions.
- Kept `awf start` behavior unchanged.

## Green Validation

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q
```

Result:

- `52 passed`

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/cli/test_package_build_contents.py
```

Result:

- Passed

```bash
git diff --check development
```

Result:

- Passed

Clean tracked-worktree validation:

- `uv sync --python 3.12 --extra dev` passed in a temporary clean worktree.
- `.venv/bin/python -m build` passed in a temporary clean worktree after
  installing the same `build` package used by CI.
