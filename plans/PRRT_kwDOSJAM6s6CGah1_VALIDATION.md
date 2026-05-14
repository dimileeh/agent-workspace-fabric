# PRRT_kwDOSJAM6s6CGah1 Validation

Plan reference: `PRRT_kwDOSJAM6s6CGah1_PLAN.md`

## Requirement Status

- Complete: Added regression coverage proving `_fail_stranded_workspace()` skips
  terminal failure when the execution claim is refreshed before the transition.
- Complete: Preserved failure-causality behavior by loading causality before the
  guarded transition and only applying row failure fields after a successful
  transition.
- Complete: Replaced the unguarded runtime-stranding failure transition with
  `transition_if_current()` plus status-specific claim predicates.
- Complete: Preserved behavior for statuses without execution or monitor claims
  by returning an empty extra-condition tuple.
- Complete: Ran the focused worker regression test and adjacent stale-active
  claim regression.

## Evidence

Files changed:

- `src/awf/control/worker.py`
- `tests/unit/control/test_worker.py`
- `plans/PRRT_kwDOSJAM6s6CGah1_PLAN.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "runtime_stranding_failure_transition_rechecks_refreshed_claim"` failed before the implementation, with `_fail_stranded_workspace()` still calling `transition()`.
- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker.py -q -k "runtime_stranding_failure_transition_rechecks_refreshed_claim or stale_active_execution_failure_transition_rechecks_refreshed_claim"` passed.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker.py tests/unit/control/test_worker.py` passed.
- `uv run --python 3.12 --extra dev mypy src/awf` passed.

No remaining gaps.
