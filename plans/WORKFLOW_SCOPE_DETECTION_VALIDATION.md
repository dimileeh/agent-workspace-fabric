# Workflow Scope Detection Validation

Plan reference: `plans/WORKFLOW_SCOPE_DETECTION_PLAN.md`

## Requirement Status

- Add regression coverage for alternate workflow-scope rejection wording:
  Complete. Added parametrized coverage for "workflows permission is required",
  "must have workflow permission", and "workflow scope required" push output
  variants.
- Preserve known GitHub detection and workflow path extraction:
  Complete. Existing git-push result regression remains in the focused test file
  and still passes.
- Avoid broad false positives:
  Complete. Added a regression that generic workflow validation text without
  workflow-scope permission language is ignored; the detector also requires
  push-rejection or workflow-file context.
- Keep downstream result handling unchanged:
  Complete. The production change is limited to `_workflow_scope_push_block`;
  existing `GITHUB_WORKFLOW_SCOPE_REQUIRED` result handling tests pass.

## Evidence

Files changed:

- `src/awf/runtime/pr_monitor_runner/remote_ops.py`
- `tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
- `plans/WORKFLOW_SCOPE_DETECTION_PLAN.md`
- `plans/WORKFLOW_SCOPE_DETECTION_VALIDATION.md`

Commands run:

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py -q`
  - Before implementation: failed on the three new alternate-message cases.
  - After implementation: passed, `11 passed in 6.36s`.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor_runner/remote_ops.py tests/unit/runtime/test_pr_monitor_runner_parts/test_pr_monitor_runner_part_006.py`
  - Passed.

Full AWF/GitHub validation was not run in the agent phase. AWF owns broad
validation, provenance, and merge gating after agent completion.
