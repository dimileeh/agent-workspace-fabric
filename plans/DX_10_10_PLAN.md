# Plan: DX 10/10 Remediation

## Summary

Resolve the top developer-experience gaps found in the current branch by
making first-run docs canonical, separating local health from release
readiness in CLI copy, replacing flattened dense pretty output with curated
terminal views, making local console discovery work in smoke reports, adding
upgrade/release docs, and polishing existing-profile onboarding.

## Implementation

- Make `docs/QUICKSTART.md` the canonical first-run guide; keep
  `docs/START_HERE.md` as a compatibility pointer; add `CHANGELOG.md` and
  `docs/UPGRADE.md`; link them from README.
- Update `awf init <path>` so existing `.awf/workspace.yml` projects recommend
  preview/smoke commands instead of suggesting profile creation.
- Add a compact pretty renderer for Core release readiness and wire it into
  `awf service readiness --format pretty`.
- Add `awf service release-readiness` as a clearer alias while preserving the
  existing `awf service readiness` command.
- Add a compact pretty renderer for `awf profile preview --format pretty`.
- Make smoke reports infer and probe `http://localhost:3000` as the default
  local console URL, while preserving explicit `AWF_CONSOLE_URL` overrides.
- Add `AWF_CONSOLE_URL=http://localhost:3000` to `.env.example`.

## Tests

- Add or update unit tests for existing-profile `awf init` guidance, service
  readiness pretty output and alias, profile preview pretty output, smoke
  console probing, and public docs discoverability.
- Validate with focused pytest, ruff, and mypy commands over the touched
  CLI/service/docs tests.

## Assumptions

- JSON output remains stable for automation.
- The console is reported and probed but not started by smoke.
- Release-readiness SLO thresholds and policies are not changed.
