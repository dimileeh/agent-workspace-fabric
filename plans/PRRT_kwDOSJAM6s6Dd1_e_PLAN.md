**Topic:** PRRT_kwDOSJAM6s6Dd1_e running PR-monitor salvage transition

## Context

The review thread reports that preserved active-execution salvage can recover a
`running` workspace with an already-open PR and call
`WorkspaceRepository.transition(..., to=WorkspaceStatus.monitoring_pr)`, but the
workspace state machine does not currently allow `running -> monitoring_pr`.

## Plan

1. Add regression coverage that proves a preserved `running` workspace whose
   remote branch resolves to an open PR can attach the PR monitor after restart.
2. Add the explicit state-machine allow-list coverage for
   `running -> monitoring_pr`.
3. Update `src/awf/control/state_machine.py` so `running` can transition to
   `monitoring_pr` for this salvage handoff.
4. Run the targeted state-machine and worker regression tests.
5. Validate the implementation against this plan in a matching validation doc.

## Validation Targets

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py::TestValidTransitions::test_allowed -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pushed_branch_open_pr_attaches_one_monitor_after_restart" -q
```
