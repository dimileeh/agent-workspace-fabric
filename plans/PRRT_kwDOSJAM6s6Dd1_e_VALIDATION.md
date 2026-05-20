**Topic:** PRRT_kwDOSJAM6s6Dd1_e running PR-monitor salvage transition

## Plan Validation

- Complete: Added regression coverage for a preserved `running` workspace whose
  remote branch resolves to an open PR during restart recovery.
- Complete: Added state-machine allow-list coverage for
  `running -> monitoring_pr`.
- Complete: Updated `src/awf/control/state_machine.py` so the repository
  transition used by `_attach_preserved_active_pr_monitor` succeeds for
  `running` workspaces.

## Evidence

Failing-before checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py::TestValidTransitions::test_allowed -q
# failed: TestValidTransitions.test_allowed[running-monitoring_pr]

uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pushed_branch_open_pr_attaches_one_monitor_after_restart" -q
# failed: InvalidWorkspaceTransitionError: running -> monitoring_pr
```

Passing-after checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_state_machine.py::TestValidTransitions::test_allowed -q
# 29 passed

uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -k "preserved_active_pushed_branch_open_pr_attaches_one_monitor_after_restart" -q
# 2 passed, 211 deselected

uv run --python 3.12 --extra dev ruff check src/awf/control/state_machine.py tests/unit/control/test_state_machine.py tests/unit/control/test_worker.py
# All checks passed
```
