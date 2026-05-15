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
