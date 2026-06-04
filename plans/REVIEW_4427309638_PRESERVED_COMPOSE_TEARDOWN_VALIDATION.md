# Review 4427309638 Preserved Compose Teardown Validation

Plan reference:
`plans/REVIEW_4427309638_PRESERVED_COMPOSE_TEARDOWN_PLAN.md`

## Requirement Status

- Complete: Added a regression test proving a preserved row-backed
  single-workspace GC run still invokes the compose teardown callback.
- Complete: Preserved rows remain out of `plan.candidates`, and their pressure
  directories are not deleted.
- Complete: The fallback candidate uses stored workspace compose metadata when
  the workspace row exists.
- Complete: The existing missing-row fallback behavior remains covered by the
  monitor completion no-candidate tests.
- Complete: Only focused checks were run; broad AWF/GitHub validation was left
  to AWF after agent completion.

## Evidence

Files changed:

- `src/awf/service/gc.py`
- `tests/unit/service/test_gc_parts/test_gc_part_001.py`
- `plans/REVIEW_4427309638_PRESERVED_COMPOSE_TEARDOWN_PLAN.md`
- `plans/REVIEW_4427309638_PRESERVED_COMPOSE_TEARDOWN_VALIDATION.md`

Focused checks:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_tears_down_compose_for_preserved_workspace -q
```

Result: failed before implementation because `result.compose_teardowns` did not
contain the preserved workspace id. Passed after implementation: `1 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_tears_down_compose_for_preserved_workspace tests/unit/service/test_gc_parts/test_gc_part_001.py::test_single_workspace_gc_ignore_retention_still_preserves_unmerged_workspace -q
```

Result: `2 passed`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_completion_gc.py -q -k "empty_plan or auth_overlay"
```

Result: `4 passed, 11 deselected`.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/service/gc.py tests/unit/service/test_gc_parts/test_gc_part_001.py
```

Result: passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/service/gc.py
```

Result: passed.

Full AWF/GitHub validation, full repository tests, and full coverage gates were
not run in the agent phase per the workspace contract.

## Remaining Gaps

None for Cursor review comment 4427309638. The pre-existing local commit on
this branch already addressed the second Cursor thread about auth-overlay
unmounts in no-candidate GC plans.
