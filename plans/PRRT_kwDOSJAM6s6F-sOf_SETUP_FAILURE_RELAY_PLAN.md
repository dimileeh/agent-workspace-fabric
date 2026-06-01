# PRRT_kwDOSJAM6s6F-sOf Setup Failure Relay Plan

## Problem Statement and Scope

Review thread `PRRT_kwDOSJAM6s6F-sOf` reports that PR monitor handoff setup
command failures are reclassified as `PR_ADOPTION_MONITOR_UNAVAILABLE` when
the setup helper's detailed and fallback `_mark_failed` calls both raise. The
generic outer handoff catch then persists the persistence error text instead of
the original setup failure message.

Scope is limited to monitor handoff setup failure propagation and focused unit
coverage for that path.

## Requirements Checklist

- Add regressions proving setup command failures remain monitor setup failures
  when local setup failure persistence attempts raise before the outer handoff
  helpers retry the transition.
- Preserve existing successful setup, monitor-factory, and single-fallback
  setup failure behavior.
- Keep persisted failure messages redacted and based on the original setup
  command failure, not the secondary persistence exception.
- Run only focused checks; full AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add failing regression coverage in the focused monitor handoff setup test
   file.
2. Run the initial focused regression and confirm it fails against the current
   behavior.
3. Add a setup-specific exception/payload relay from
   `monitor_handoff_setup.py` to the outer handoff helper catch paths.
4. Update the two outer monitor handoff helpers to retry the original setup
   failure payload instead of classifying it as monitor unavailable.
5. Run the focused regression and relevant monitor handoff setup tests.

## Verification Commands

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup::test_sync_feature_pr_handoff_setup_mark_failed_double_error_preserves_setup_failure -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py::TestExecutorMonitorHandoffSetup -q
uv run --python 3.12 --extra dev ruff check src/awf/control/executor/monitor_handoff.py src/awf/control/executor/monitor_handoff_setup.py tests/unit/control/test_executor_error_paths_parts/test_executor_error_paths_part_013.py
```

Pass criteria: the regression fails before implementation, passes after
implementation, the focused class remains green, and ruff reports no issues.
