# Issue 307 Validation

## Implementation Status

Complete for the required scope and Stretch 2 warning.

- Past-settle remonitor with a non-empty `reason` now persists a pending operator
  hint in monitor state and re-arms the existing initial-review/non-check
  reviewer settle markers.
- `pr_monitor.decide()` blocks merge while the hint is pending and returns a
  dedicated operator-hint repair action.
- The runner dispatches the operator-hint repair prompt, clears the hint only
  after a pushed repair pass, and routes no-fix/needs-human outcomes into a
  merge-blocking human wait.
- `WorkspaceControlResponse` now includes `warnings`, and past-settle remonitor
  returns `REMONITOR_PAST_SETTLE`.
- `openapi.json` was regenerated.

## Deferred Stretch

- `block_auto_merge_until` was not implemented. The cooldown uses the existing
  monitor grace/settle configuration and persisted state machinery.

## Focused Checks Run

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py::TestOperatorHints tests/unit/runtime/test_monitor_prompts.py::TestOperatorHintPrompt -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_response_payload_alignment.py::test_control_rest_matches_mcp_structured_content_for_registry tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_remonitor_workspace_tool_returns_control_response tests/unit/mcp/test_mcp_operator_surfaces_parts/test_mcp_operator_surfaces_part_003.py::TestMcpOperatorSurfaceParityPart002::test_remonitor_workspace_with_idempotency_key_replays_on_duplicate -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_monitor_prompts.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py::TestOperatorHints tests/unit/runtime/test_pr_monitor_initial_review_grace.py tests/unit/runtime/test_pr_monitor_non_check_reviewer_settle.py::test_non_check_reviewer_wait_starts_for_green_pr_without_visible_reviewer_check -q`
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/pr_monitor.py src/awf/runtime/operator_hints.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/loop.py src/awf/service/controls.py src/awf/service/controls_helpers.py src/awf/api/schemas.py tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`
- `uv run --python 3.12 --extra dev ruff format --check src/awf/runtime/pr_monitor.py src/awf/runtime/operator_hints.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/loop.py src/awf/service/controls.py src/awf/service/controls_helpers.py src/awf/api/schemas.py tests/unit/runtime/test_pr_monitor_operator_hints.py tests/unit/runtime/test_monitor_prompts.py tests/unit/runtime/test_pr_monitor_parts/test_pr_monitor_part_001.py tests/unit/api/test_workspace_controls_idempotency_parts/test_workspace_controls_idempotency_part_001.py`
- `uv run --python 3.12 --extra dev mypy src/awf/runtime/pr_monitor.py src/awf/runtime/operator_hints.py src/awf/runtime/monitor_prompts.py src/awf/runtime/pr_monitor_runner/operator_hints.py src/awf/runtime/pr_monitor_runner/lifecycle.py src/awf/runtime/pr_monitor_runner/loop.py src/awf/service/controls.py src/awf/service/controls_helpers.py src/awf/api/schemas.py`
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`

Full AWF/GitHub validation and merge gating remain owned by AWF after this
agent phase.
