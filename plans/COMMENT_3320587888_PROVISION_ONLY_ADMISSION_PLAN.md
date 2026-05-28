# Comment 3320587888 Provision-Only Admission Plan

## Problem Statement And Scope

PR review thread `PRRT_kwDOSJAM6s6FhF4U` reports that a worker configured without
an executor can advertise requested provisioning slots via
`_requested_provision_slots()`, but the subsequent claim path rejects every
requested workspace when `max_concurrent_executions=0` because admission row
slots are derived from execution capacity.

Scope is limited to requested-workspace claim admission for provision-only
workers in `src/awf/control/worker/claims.py` and focused unit coverage in the
existing worker admission tests.

## Requirements Checklist

- Add a regression proving a no-executor worker with
  `max_concurrent_executions=0` can claim and provision requested work.
- Preserve the execution-slot row admission gate for workers that do have an
  executor configured.
- Keep local-capacity and non-local-capacity requested claim paths consistent
  for provision-only workers.
- Run only focused validation; full AWF/GitHub validation remains managed after
  agent completion.

## Implementation Steps

1. Add failing focused regression coverage in
   `tests/unit/control/test_worker_scheduler_admission.py`.
2. Update requested claim admission so no-executor workers do not apply the
   execution row-slot gate, while executor-backed workers still do.
3. Run the targeted new tests and a narrow ruff check over touched files.
4. Record validation evidence in the matching validation document.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "provision_only"`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/control/worker/claims.py tests/unit/control/test_worker_scheduler_admission.py`
  passes.
- No broad repository test, coverage, frontend build, push, or branch operation
  is run by the agent.
