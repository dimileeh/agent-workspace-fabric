# PR Monitor Adoption Model/Effort Parity Plan

## Problem Statement And Scope

PR monitor adoption currently lacks the model and effort selection parity that
workspace creation already exposes. Operators must patch adopted workspace
`task_policy` manually after adoption. This slice adds model/effort selection
only to PR monitor adoption surfaces: REST, CLI, and MCP where the adoption tool
already exists.

Out of scope: workspace creation changes, scheduling changes, provider recovery
changes, PR monitor execution rewrites, merge/comment/CI repair changes, and
coverage policy changes.

## Requirements Checklist

- Extend `PullRequestMonitorAdoptionRequest` with optional `model` and
  optional `effort`.
- Persist provided values into adopted workspace `task_policy` as
  `agent_model` and `agent_effort`.
- If `model` is provided without `effort`, resolve `agent_effort` through the
  existing default model/effort policy; Codex model adoption should resolve to
  `xhigh`.
- Preserve explicit `effort` values exactly.
- Preserve existing no-model/no-effort adoption behavior without adding policy
  keys.
- Include resolved model/effort policy in live adoption idempotency/conflict
  checks.
- Add `--model` and `--effort` to `awf workspace adopt-pr` and send only
  operator-provided fields.
- Add matching MCP schema/handler fields if a PR adoption tool exists.
- Rely on existing task-policy observability for `workspace show` and task
  surfaces.

## Implementation Steps

1. Inspect current adoption schema, REST route, service creation path, live
   adoption comparison, CLI command, and MCP adoption tool.
2. Add failing focused tests for schema/OpenAPI, service persistence/defaulting,
   service idempotency/conflict, CLI payload/help, and MCP schema/handler parity.
3. Extend the request schema with optional `model` and `effort`.
4. Add a small service-layer policy normalizer that maps adoption request values
   to `agent_model`/`agent_effort`, using existing adapter defaults for
   model-only effort resolution.
5. Merge the normalized policy into new adopted workspace `task_policy` only
   when needed.
6. Compare requested resolved policy against existing live adoption policy when
   deciding idempotent attach versus conflict.
7. Wire CLI flags into the REST payload.
8. Wire MCP fields into the tool schema and handler if the tool exists.
9. Run focused validation, then broader type/lint checks as requested.

## Verification Commands And Pass Criteria

Focused tests must pass:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli/test_cli.py tests/unit/mcp/test_mcp_server.py -q
```

Lint and type checks must pass:

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests/unit/api/test_pr_monitor_adoption.py tests/unit/service/test_pr_monitor_adoption.py tests/unit/cli tests/unit/mcp
uv run --python 3.12 --extra dev mypy src/awf
```

If OpenAPI artifacts are affected, run the drift check and update generated
artifacts only through the repository workflow:

```bash
python scripts/generate_openapi.py --check
```
