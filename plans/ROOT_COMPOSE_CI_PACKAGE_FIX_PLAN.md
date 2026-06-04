# Root Compose CI Package Fix Plan

## Summary

Fix the immediate PR #403 CI failure caused by packaging an ignored/generated
console file (`apps/console/next-env.d.ts`) that exists locally but is absent in
clean GitHub Actions checkouts.

## Scope

- Add a package-content regression test that console package inputs are tracked
  source files/directories, not ignored local artifacts.
- Remove `apps/console/next-env.d.ts` from wheel and sdist package inputs.
- Treat `apps/console/next-env.d.ts` as an excluded generated console artifact.
- Keep the package path focused; do not change `awf start` behavior.

## Validation

- Confirm the new regression fails before the manifest fix.
- Run package content tests after the fix.
- Run a release-style package build from a clean tracked checkout.
- Run `git diff --check development`.
- Commit and push the fix to PR #403.
