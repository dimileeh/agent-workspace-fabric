# Review PRRT_kwDOSJAM6s6FidBZ Default Local Admission Validation

Plan reference:
`plans/REVIEW_PRRT_kwDOSJAM6s6FidBZ_DEFAULT_LOCAL_ADMISSION_PLAN.md`

## Requirement Status

- Regression for default workers counting active `node_id="local"` rows:
  Complete. Added
  `test_default_worker_counts_local_node_active_rows_as_occupied`.
- Preserve named-node and remote-node behavior:
  Complete. Re-ran focused neighboring admission tests for null-node legacy
  rows and remote named-node exclusion.
- Keep fix scoped to admission row counting:
  Complete. Code change is limited to effective node-id normalization in
  `src/awf/control/worker/admission.py`.
- Run only targeted validation:
  Complete. No broad AWF/GitHub-owned validation suite, full coverage gate, or
  frontend build was run in the agent phase.

## Evidence

Changed files:

- `src/awf/control/worker/admission.py`
- `tests/unit/control/test_worker_scheduler_admission.py`

Focused failing test before implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "default_worker_counts_local_node_active_rows"
```

Result before fix: failed with `assert 1 == 0`, proving the default worker
admitted requested work despite an active `node_id="local"` row filling the
only execution slot.

Focused validation after implementation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_worker_scheduler_admission.py -q -k "default_worker_counts_local_node_active_rows or null_node_worker_admission_ignores_active_rows_on_named_nodes or named_node_worker_counts_null_node_provisioning_rows_as_occupied"
```

Result: `3 passed, 13 deselected`.

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check src/awf/control/worker/admission.py tests/unit/control/test_worker_scheduler_admission.py
```

Result: `All checks passed!`

Full AWF/GitHub validation is managed by AWF after agent completion.
