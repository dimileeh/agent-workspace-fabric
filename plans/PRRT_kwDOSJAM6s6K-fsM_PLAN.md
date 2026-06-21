# PRRT_kwDOSJAM6s6K-fsM sync handoff mirror hook repair plan

Review thread `PRRT_kwDOSJAM6s6K-fsM` reports that sync PR monitor handoff
setup can poison the shared mirror by setting `core.hooksPath` and then failing
before any mirror-hook repair runs.

## Scope

- Inspect the existing feature-workspace mirror hook repair path and sync
  monitor handoff setup path.
- Add a focused regression for sync handoff setup failure that verifies mirror
  hook repair runs after setup/pre_agent failure and before the workspace is
  marked failed.
- Implement the smallest handoff setup change using the existing mirror repair
  helper and reason-code behavior.
- Run only focused tests/lint for the touched files. Full AWF/GitHub validation
  remains managed by AWF after this agent phase.

## Acceptance Criteria

- `sync_feature_pr` / `sync_release_pr` handoff setup repairs the mirror before
  setup and after setup/pre_agent failure.
- A failed repair marks the workspace failed with the existing mirror hook repair
  reason code and blocks monitor handoff.
- Existing setup failure classification and messages are preserved when mirror
  repair succeeds.
