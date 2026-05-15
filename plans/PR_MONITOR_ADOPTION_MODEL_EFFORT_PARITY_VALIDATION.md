# PR Monitor Adoption Model/Effort Parity Validation

Plan reference: `plans/PR_MONITOR_ADOPTION_MODEL_EFFORT_PARITY_PLAN.md`

## Requirement Status

- Extend `PullRequestMonitorAdoptionRequest` with optional `model` and
  optional `effort`: Complete.
- Persist requested values into adopted workspace `task_policy` as
  `agent_model` and `agent_effort`: Complete.
- Default `agent_effort` for model-only adoption through existing agent
  defaults, with Codex resolving to `xhigh`: Complete.
- Preserve explicit `effort` values: Complete.
- Preserve no-model/no-effort adoption behavior without adding policy keys:
  Complete.
- Include resolved model/effort in live adoption idempotency/conflict checks:
  Complete.
- Add `--model` and `--effort` to `awf workspace adopt-pr` and send only
  operator-provided fields: Complete.
- Add MCP schema/handler parity for the existing PR adoption tool: Complete.
- Preserve observability through existing task-policy-derived fields: Complete.

## Evidence

Files changed:

- `src/awf/api/schemas.py`
- `src/awf/service/pr_monitor_adoption.py`
- `src/awf/cli/main.py`
- `src/awf/mcp/server.py`
- `openapi.json`
- `tests/unit/api/test_pr_monitor_adoption.py`
- `tests/unit/service/test_pr_monitor_adoption.py`
- `tests/unit/cli/test_cli.py`
- `tests/unit/mcp/test_mcp_server.py`

Validation commands:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py -q
```

Result: Passed, 302 tests.

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli tests/unit/mcp
```

Result: Passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf
```

Result: Passed.

```bash
uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check
```

Result: Passed after regenerating `openapi.json`.

## Notes

The repository-documented plain `python scripts/generate_openapi.py --check`
failed in this workspace because the ambient Python interpreter did not have
FastAPI installed. The equivalent `uv run --python 3.12 --extra dev` command
was used for generation and final drift validation.

## Attempt 1 Validation Repair

The AWF full validation pass found one stale surface contract registry row:
`adopt_pr_monitor` REST metadata did not include the newly added `model` and
`effort` body fields. The registry row now declares those fields for REST and
MCP request metadata, and the matching `--model` / `--effort` CLI options.

Repair validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/contracts/test_surface_metadata_alignment.py::test_rest_route_metadata_matches_registry tests/unit/contracts/test_surface_metadata_alignment.py::test_cli_command_shape_matches_registry tests/unit/contracts/test_surface_metadata_alignment.py::test_mcp_tool_schema_matches_registry -q
```

Result: Passed, 101 tests.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli tests/unit/cli tests/unit/contracts/_capabilities.py
```

Result: Passed.

```bash
uv run --python 3.12 --extra dev mypy src/awf/cli
```

Result: Passed.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/cli -q
```

Result: Passed, 227 tests.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/control/test_test_quality_guardrails_self.py -q
```

Result: Passed, 1 test.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py -q
```

Result: Passed, 302 tests.

## Attempt 2 Coverage Repair

The next validation pass reported local full-suite combined statement/branch
coverage at 98.55% against the configured 99% threshold. The coverage report
showed the largest remaining debt in pre-existing executor and validation
branches outside this adoption slice. This repair keeps the threshold unchanged
and adds focused coverage for the adoption model/effort branches that remained
uncovered, plus the MCP artifact binary scan branch exercised by the same
surface validation bundle.

Changed files:

- `plans/PR_MONITOR_ADOPTION_MODEL_EFFORT_PARITY_PLAN.md`
- `plans/PR_MONITOR_ADOPTION_MODEL_EFFORT_PARITY_VALIDATION.md`
- `tests/unit/service/test_pr_monitor_adoption.py`
- `tests/unit/mcp/test_mcp_server.py`

Repair validation:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_model_only_policy_omits_effort_when_agent_default_has_no_effort tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_supersede_previous_adoption_preserves_nonmatching_task_fields tests/unit/service/test_pr_monitor_adoption.py::TestPullRequestMonitorAdoptionService::test_supersede_previous_adoption_tolerates_missing_task_row tests/unit/mcp/test_mcp_server.py::TestReadWorkspaceArtifact::test_binary_artifact_containing_provider_env_secret_is_blocked -q
```

Result: Passed, 4 tests.

```bash
uv run --python 3.12 --extra dev ruff check src/awf/cli tests/unit/cli
uv run --python 3.12 --extra dev mypy src/awf/cli
uv run --python 3.12 --extra dev pytest tests/unit/cli -q
uv run --python 3.12 --extra dev pytest tests/unit/control/test_test_quality_guardrails_self.py -q
uv run --python 3.12 --extra dev pytest tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py -q
uv run --python 3.12 --extra dev ruff check tests/unit/service/test_pr_monitor_adoption.py tests/unit/mcp/test_mcp_server.py
```

Result: Passed. The adoption/API/MCP bundle now runs 306 tests.
