# Issue 311 Implementation Plan: Official xAI Grok Build Runtime

## Scope

Add the official xAI Grok Build CLI as a new AWF `AgentRuntime.grok`, mirroring
the existing `gemini` and `opencode` runtime wiring. This integration targets
the official xAI CLI installed from `https://x.ai/cli/install.sh`, not the
community `superagent-ai/grok-cli`.

## Documentation Contract

- xAI headless docs (`https://docs.x.ai/build/cli/headless-scripting`) document
  `grok -p`, `-m/--model`, `--output-format`, `--always-approve`,
  `--no-alt-screen`, and `--no-auto-update`.
- xAI enterprise docs (`https://docs.x.ai/build/enterprise`) document
  `XAI_API_KEY` as the script/CI/headless authentication path.
- xAI model docs (`https://docs.x.ai/developers/models/grok-build-0.1`) document
  `grok-build-0.1` as the Grok Build coding model, with aliases. AWF will use
  `grok-build-0.1` as the default model.
- The official installer supports a positional version argument; the runtime
  image will pin `GROK_VERSION` to the current stable version observed during
  implementation (`0.2.14`) and install with `bash -s "${GROK_VERSION}"`.

## Implementation Checklist

1. Add failing tests for the Grok adapter, registry, defaults, provider
   inference/failure classification, readiness, auth env propagation, usage
   unsupported handling, CLI acceptance, doctor/smoke strings, and Dockerfile
   install contract.
2. Add `AgentRuntime.grok = "grok"` and a `src/awf/adapters/grok.py` adapter.
   The adapter will keep AWF's prompt off Docker argv by using a shell wrapper
   that reads stdin into a temporary file/variable and invokes:
   `grok -p "$prompt" --always-approve --no-alt-screen --no-auto-update -m <model>
   --output-format plain`.
3. Document effort mapping as model-based/no-op: Grok Build has no portable CLI
   reasoning-effort flag, so AWF does not invent one. All effort levels keep the
   selected model.
4. Register the adapter and add defaults:
   `AgentDefaults(model="grok-build-0.1", effort="xhigh")`.
5. Wire `grok`/`xai` through provider failure inference, recovery provider maps,
   readiness provider names, selected-agent preflight, doctor labels/reason
   text, strict-provider help, smoke messaging, MCP provider help, and usage
   collection as explicitly unsupported by ccusage.
6. Add `XAI_API_KEY` to service-to-agent env placeholder propagation and
   readiness secret redaction. Do not add a Grok credential directory mount.
7. Update `docker/agent-runtime.Dockerfile` to install the official pinned Grok
   Build binary with `curl -fsSL https://x.ai/cli/install.sh | bash -s
   "${GROK_VERSION}"` and `GROK_BIN_DIR=/usr/local/bin`; assert `grok --version`.
8. Regenerate `docs/REASON_CATALOG.md` if doctor reason text changes require it.
9. Run focused unit tests and lint/type checks for touched surfaces. Full
   real-CLI end-to-end validation is deferred because it needs a rebuilt
   agent-runtime image plus a real `XAI_API_KEY`.

## Validation Plan

Focused checks will cover:

- Adapter/defaults/registry tests in `tests/unit/adapters/test_adapters.py`.
- Provider failure tests in `tests/unit/adapters/test_provider_failures.py`.
- Provider readiness tests in `tests/unit/service/test_provider_readiness_parts`.
- Provider recovery tests in the focused recovery helper suites.
- Auth env propagation tests in `tests/unit/node`.
- Usage store/collector tests for the unsupported ccusage source path.
- CLI/service/doctor/smoke/Dockerfile targeted tests.

The final validation document will record commands run and explicitly note that
real Grok CLI execution is deferred to an environment with `grok`,
`XAI_API_KEY`, and the rebuilt runtime image.
