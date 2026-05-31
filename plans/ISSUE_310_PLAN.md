# Issue 310 Cursor Agent Runtime Plan

## Contract

Add Cursor CLI as a new AWF `AgentRuntime` named `cursor`. The adapter is
additive and must not change existing `codex`, `claude_code`, `gemini`, or
`opencode` behavior.

Cursor runs through the documented `cursor-agent` binary. AWF will keep the
real prompt on stdin, following the existing runner convention, and the adapter
will return argv for print mode: `cursor-agent -p --force ... --output-format
text`. `--force` is mandatory because AWF needs non-interactive writes inside
the workspace container.

Official Cursor CLI references used by the adapter docstring:

- https://cursor.com/docs/cli/overview
- https://cursor.com/docs/cli/headless

## Implementation Steps

1. Write failing unit tests for Cursor adapter construction, registry/defaults,
   provider readiness, auth env forwarding, failure classification, recovery,
   Dockerfile install wiring, and docs/help surfaces that enumerate providers.
2. Add `AgentRuntime.cursor`, `CursorAdapter`, registry import, and defaults.
   Default model: `sonnet-4-thinking`; default effort: `xhigh`.
3. Wire `CURSOR_API_KEY` through readiness and workspace environment handling.
   Do not add a Cursor home-directory auth mount and never log the raw key.
4. Add `cursor-agent` installation to `docker/agent-runtime.Dockerfile` through
   the official installer. Document that the installer tracks Cursor's current
   CLI because no stable version pin is documented.
5. Wire provider failures/recovery, doctor, smoke, usage/provider summaries,
   CLI/MCP help text, current operator docs, and OpenAPI enum drift.
6. Validate with focused commands only. Broad AWF/GitHub gates, full coverage,
   full frontend builds, and real Cursor CLI/API validation are deferred to AWF
   after agent completion or to a human with a rebuilt runtime image and real
   `CURSOR_API_KEY`.

## Focused Validation Targets

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py tests/unit/adapters/test_provider_failures.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py -q`
- `uv run --python 3.12 --extra dev pytest tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_003.py tests/unit/service/test_doctor.py tests/unit/service/test_usage_store.py tests/unit/service/test_usage_collection.py tests/unit/test_agent_runtime_dockerfile.py -q`
- Focused `ruff`, `mypy`, and `python scripts/generate_openapi.py --check` for
  touched backend modules, narrowing further if a command proves too broad for
  this AWF workspace contract.

## Deferred

End-to-end validation with the real `cursor-agent` binary and real
`CURSOR_API_KEY` is intentionally deferred. The sandbox is not expected to have
the CLI installed or a Cursor API key available.
