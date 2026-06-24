# Bitbucket CLEAN Queue Attention Validation

Plan reference: `plans/BITBUCKET_CLEAN_QUEUE_ATTENTION_PLAN.md`

## Requirement Status

- Complete: Added a focused regression proving Bitbucket `CLEAN` preserves the
  merge-block marker and `awaiting_human_since` during queue-attention handling.
- Complete: Preserved GitHub behavior; the existing GitHub `CLEAN` queue-wait
  test still clears resolved merge-block attention.
- Complete: Kept changes scoped to merge-block attention queue verdict callers,
  focused runtime tests, and plan/validation artifacts.
- Complete: Ran only focused local checks. Full AWF/GitHub validation is managed
  after agent completion.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/merge_attention.py`
- `src/awf/runtime/pr_monitor_runner/gates.py`
- `src/awf/runtime/pr_monitor_runner/merge_loop.py`
- `tests/unit/runtime/test_merge_queue_ordering.py`
- `plans/BITBUCKET_CLEAN_QUEUE_ATTENTION_PLAN.md`
- `plans/BITBUCKET_CLEAN_QUEUE_ATTENTION_VALIDATION.md`

Focused checks run:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_bitbucket_clean_preserves_branch_protection_marker_on_merge_queue_wait -q
```

Initial red result before implementation: failed with
`TypeError: _clear_or_preserve_merge_attention_for_queue_wait() got an unexpected keyword argument 'forge'`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_merge_queue_wait_when_forge_resolved tests/unit/runtime/test_merge_queue_ordering.py::test_bitbucket_clean_preserves_branch_protection_marker_on_merge_queue_wait -q
```

Result: passed, `2 passed`.

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_preserved_on_merge_queue_wait_when_forge_blocked \
  tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_merge_queue_wait_when_forge_resolved \
  tests/unit/runtime/test_merge_queue_ordering.py::test_bitbucket_clean_preserves_branch_protection_marker_on_merge_queue_wait \
  tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_cleared_on_reviewer_settle_wait_when_forge_resolved \
  tests/unit/runtime/test_merge_queue_ordering.py::test_branch_protection_marker_preserved_on_initial_grace_wait_when_recheck_errors \
  -q
```

Result: passed, `5 passed`.

```bash
uv run --python 3.12 --extra dev ruff format src/awf/runtime/pr_monitor_runner/gates.py
```

Result: reformatted one file after the commit hook reported the focused format
check failure.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/merge_attention.py src/awf/runtime/pr_monitor_runner/gates.py src/awf/runtime/pr_monitor_runner/merge_loop.py tests/unit/runtime/test_merge_queue_ordering.py
```

Result: passed.
