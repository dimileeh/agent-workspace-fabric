# PR403 Review Comments Plan

## Problem Statement

PR #403 has unresolved review threads while the long full-coverage CI job is
still running. Address each thread according to merit without expanding into
the separate `awf start` route that is being developed elsewhere.

## Scope And Classification

- Legitimate fix: propagate the local Compose default API token into
  `ServiceSettings.api_token` so host CLI calls can authenticate against a raw
  `docker compose up --build` stack.
- Already handled but worth regression coverage: root `.dockerignore` excludes
  console `node_modules`, `.next`, and other generated artifacts from the
  console image build context.
- Legitimate fix: copy `next.config.ts` into the console runtime image.
- Legitimate fix: make the console runtime container exec `next` directly
  instead of running through `npm`.
- Legitimate fix: treat legacy/root/template env paths as regular files before
  reading them.
- Legitimate fix: escape `$` when migrating quoted dotenv values so
  `python-dotenv` does not interpolate secrets.
- Out of scope by user direction: starting the console from `awf start` /
  `awf service bootstrap`.
- False positive removal request: the `apps/console/lib` sdist force-include is
  intentionally duplicated to keep hatch wheel-from-sdist builds complete; add a
  comment instead of removing it.

## Implementation Steps

1. Add failing unit tests for the API token default, env migration safety,
   dotenv dollar escaping, and console Dockerfile runtime behavior.
2. Implement the smallest source changes needed to satisfy those tests.
3. Add or retain static evidence for already-handled Docker ignore behavior and
   clarify the intentional hatch force-include.
4. Validate with the narrow affected tests plus Dockerfile/build sanity checks.
5. Commit, push the fixes, and update/resolve the PR review threads with a
   concise classification summary.

## Verification Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_config_parts/test_config_part_003.py -q -k api_token`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_env_migration.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/test_console_dockerfile.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/cli/test_package_build_contents.py -q`
- `docker build -t awf-console:ci-check -f apps/console/Dockerfile .`
- `git diff --check development`

## Pass Criteria

- Legitimate review comments have code or test coverage addressing the reported
  issue.
- False-positive or deferred comments are documented with a concrete rationale.
- The PR branch is pushed with a new fix commit.

## Assumptions/Changes

- The dollar-value fix preserves raw env values by reading AWF-managed dotenv
  files with `interpolate=False` and writing `$`-containing migrated values with
  single quotes. A quick check showed that backslash-escaping `${...}` in a
  double-quoted value is not preserved by `python-dotenv`'s default
  interpolation behavior, so the implementation targets AWF's raw-read contract
  instead of adding literal backslashes to credentials.
