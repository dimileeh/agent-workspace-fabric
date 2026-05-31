# Issue 311 Validation - Grok Build Runtime

Plan reference: `plans/ISSUE_311_PLAN.md`

## Result

Implemented the official xAI Grok Build CLI as `AgentRuntime.grok` and wired it
through AWF adapter registration, defaults, provider readiness, provider failure
classification, provider recovery, service-to-agent env propagation, doctor,
smoke, usage, CLI help, MCP readiness help, Dockerfile install, and OpenAPI.

## Plan Checklist

| Plan Item | Status | Evidence |
| --- | --- | --- |
| Add failing tests before implementation | Complete | Initial focused run failed on missing `awf.adapters.grok`, proving the adapter contract tests were red before production code existed. |
| Add `AgentRuntime.grok` and Grok adapter | Complete | `src/awf/db/enums.py` and `src/awf/adapters/grok.py` now define and register Grok. |
| Use official headless Grok invocation | Complete | Adapter launcher executes `grok -p "$prompt" "$@"` with `--always-approve`, `--no-alt-screen`, `--no-auto-update`, `--output-format plain`, and optional `-m <model>`. |
| Default model and effort mapping | Complete | `DEFAULT_AGENT_DEFAULTS[AgentRuntime.grok]` uses `grok-build-0.1`, `effort="xhigh"`. Effort is documented as model-preserving because Grok Build exposes no portable reasoning-effort flag. |
| Provider readiness and auth env | Complete | Grok readiness requires `XAI_API_KEY`; selected launch preflight also probes `command -v grok` in the runtime image. `XAI_API_KEY` is propagated as an env placeholder and redacted in readiness payloads. |
| No Grok auth directory mount | Complete | Auth mount tests assert `~/.grok` is not copied or mounted. |
| Dockerfile official installer | Complete | `docker/agent-runtime.Dockerfile` pins `GROK_VERSION=0.2.14` and installs from `https://x.ai/cli/install.sh` with `GROK_BIN_DIR=/usr/local/bin`; tests assert it does not use the community npm fork. |
| Wire operator surfaces | Complete | Doctor labels/reasons, reason catalog, smoke action text, CLI provider help, MCP provider help, and OpenAPI include Grok where applicable. |
| Usage handling | Complete | Grok is explicitly unsupported by `ccusage` for now and records `ccusage_source_unsupported` without invoking `ccusage`. |

## Validation Commands

- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py::TestGrokAdapter -q`
  - Initial TDD red state: failed with `ModuleNotFoundError: No module named 'awf.adapters.grok'`.
- `uv run --python 3.12 --extra dev pytest tests/unit/adapters/test_adapters.py tests/unit/adapters/test_provider_failures.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_001.py tests/unit/service/test_provider_readiness_parts/test_provider_readiness_part_002.py tests/unit/service/test_doctor.py tests/unit/service/test_doctor_reasons.py tests/unit/service/test_smoke_parts/test_smoke_part_001.py tests/unit/service/test_usage_collection.py tests/unit/service/test_usage_store.py tests/unit/service/test_provider_recovery_coverage_gaps.py tests/unit/cli/test_cli_parts/test_cli_part_001.py tests/unit/cli/test_cli_parts/test_cli_part_002.py tests/unit/cli/test_service_cli_parts/test_service_cli_part_003.py::test_service_provider_help_lists_codex_and_docker tests/unit/node/test_stack_launcher_parts/test_stack_launcher_part_003.py::test_compose_stack_launcher_passes_provider_auth_placeholders tests/unit/node/test_service_auth_mounts.py::test_service_auth_mounts_include_existing_host_credentials tests/unit/test_agent_runtime_dockerfile.py -q`
  - Passed: `503 passed`.
- `uv run --python 3.12 --extra dev ruff check src/awf/adapters src/awf/db/enums.py src/awf/service/provider_readiness.py src/awf/service/provider_recovery.py src/awf/service/doctor src/awf/service/smoke.py src/awf/service/usage_store.py src/awf/profiles/compose.py src/awf/cli/service_commands.py src/awf/mcp/metrics_tools.py ...focused modified tests...`
  - Passed.
- `uv run --python 3.12 --extra dev ruff format --check src/awf/adapters src/awf/db/enums.py src/awf/service/provider_readiness.py src/awf/service/provider_recovery.py src/awf/service/doctor src/awf/service/smoke.py src/awf/service/usage_store.py src/awf/profiles/compose.py src/awf/cli/service_commands.py src/awf/mcp/metrics_tools.py ...focused modified tests...`
  - Passed.
- `uv run --python 3.12 --extra dev mypy src/awf/adapters/grok.py src/awf/adapters/defaults.py src/awf/adapters/provider_failures.py src/awf/service/provider_readiness.py src/awf/service/provider_recovery.py src/awf/service/doctor src/awf/service/smoke.py src/awf/service/usage_store.py src/awf/profiles/compose.py src/awf/cli/service_commands.py src/awf/mcp/metrics_tools.py src/awf/db/enums.py`
  - Passed: `Success: no issues found in 14 source files`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Initially failed because the `AgentRuntime` enum changed.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py`
  - Regenerated `openapi.json`.
- `uv run --python 3.12 --extra dev python scripts/generate_openapi.py --check`
  - Passed.

## Deferred Validation

Real Grok CLI end-to-end execution is deferred to a human/operator environment
with a rebuilt `awf-agent-runtime` image, the official `grok` binary installed
from xAI, and a real `XAI_API_KEY`. AWF/GitHub will own the broad validation,
full coverage gate, full repository test suites, frontend build, push, PR
creation, and merge gating after this agent phase.

PR description should include: `Closes #311`.
