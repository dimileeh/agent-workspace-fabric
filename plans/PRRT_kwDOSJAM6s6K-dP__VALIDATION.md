# PRRT_kwDOSJAM6s6K-dP_ Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6K-dP__PLAN.md`

## Requirement Status

- Add a focused regression test showing mirror repository alternates cause
  `_mirror_commit_object_exists` to fail closed: Complete.
- Keep environment object lookup override stripping intact for the normal
  `cat-file` probe: Complete; existing repair-start-head coverage still passes.
- Make the implementation minimal and local to PR monitor mirror anchor
  validation: Complete.
- Do not run broad AWF or CI-equivalent validation; record focused checks only:
  Complete.

## Evidence

Changed files:

- `src/awf/runtime/pr_monitor_runner/remote_repair.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
- `plans/PRRT_kwDOSJAM6s6K-dP__PLAN.md`
- `plans/PRRT_kwDOSJAM6s6K-dP__VALIDATION.md`

Focused checks:

- Initial TDD failure confirmed:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k mirror_commit_object_exists`
  failed because `_mirror_commit_object_exists` returned `True` when the fake
  `cat-file` probe succeeded despite a mirror `objects/info/alternates` file.
- Passing regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k mirror_commit_object_exists`
  passed with `1 passed, 20 deselected`.
- Nearby repair-start-head slice:
  `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py -q -k 'repair_operation_start_head or mirror_commit_object_exists'`
  passed with `2 passed, 19 deselected`.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_repair.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_005.py`
  passed.

Full AWF/GitHub validation was not run in the agent phase; AWF owns broad
validation, provenance, logs, timeouts, and merge gating after completion.

## Gaps

None.
