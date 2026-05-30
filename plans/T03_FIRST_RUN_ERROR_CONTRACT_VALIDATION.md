# T03 First-Run Error Contract Validation

Plan reference: `plans/T03_FIRST_RUN_ERROR_CONTRACT_PLAN.md`

Source contract:

- `docs/awf-plans/ws_b459233cc6e6403c935672b8.md`
- `plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md`
- `TODO/awf-full-installer-first-run-setup-backlog.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Save implementation plan before coding | Complete | Added `plans/T03_FIRST_RUN_ERROR_CONTRACT_PLAN.md` before source implementation. |
| Add shared first-run rendering models/helpers | Complete | Added `src/awf/host_setup/rendering.py` with `FirstRunRemediation`, `FirstRunIssue`, `FirstRunPayload`, payload constructors, pretty rendering, JSON rendering, and redaction. |
| Pretty output renders success, warning, and failure payloads | Complete | `tests/unit/service/test_host_setup_rendering.py` covers success, warning, failure, and every first-run failure code. |
| JSON output exposes stable reason codes and structured remediation | Complete | Renderer and CLI tests assert top-level `reason_code`, issue `reason_code`, remediation fields, details, and next steps. |
| Add reason catalog entries for setup, source-checkout, installer, credential, client, and start failures | Complete | Updated `src/awf/service/doctor/reasons.py` and regenerated `docs/REASON_CATALOG.md`. |
| Reason catalog coverage tests for all new codes | Complete | Added first-run code coverage in `tests/unit/service/test_doctor_reasons.py`; existing docs catalog coverage remains green. |
| Redact token-looking values and provider refs from pretty and JSON output | Complete | Renderer redaction test covers GitHub/OpenAI/Anthropic/Gemini/Slack-shaped tokens, bearer/authorization text, URL credentials, sensitive keys, and `keyring://`, `env://`, `plain-file://` refs. |
| Wire setup/start placeholders through shared renderer without implementing later behavior | Complete | Updated `src/awf/cli/setup_commands.py` and `src/awf/cli/start_commands.py`; placeholders still exit 1 and remain reserved. |
| Preserve H01-H04 locked decisions and keep scope to T03 | Complete | No installer behavior, setup dry-run checks, start bootstrap wrapper, credential backend, client config writes, MCP tools, or orchestration were implemented. |

## TDD Evidence

Initial failing-test confirmation before implementation:

```text
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q
Result: failed during collection with ModuleNotFoundError: awf.host_setup.rendering

uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py -q
Result: failed during collection with ModuleNotFoundError: awf.host_setup.rendering
```

Focused post-implementation tests:

```text
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q
Result: 5 passed

uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py tests/unit/service/test_doctor_reasons.py tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py tests/unit/docs/test_catalog_coverage.py -q
Result: 16 passed
```

Focused lint/type checks:

```text
uv run --python 3.12 --extra dev ruff check src/awf/host_setup src/awf/cli/setup_commands.py src/awf/cli/start_commands.py src/awf/service/doctor/reasons.py tests/unit/service/test_host_setup_rendering.py tests/unit/service/test_doctor_reasons.py tests/unit/cli/test_setup_commands.py tests/unit/cli/test_start_commands.py
Result: All checks passed

uv run --python 3.12 --extra dev mypy src/awf/host_setup src/awf/cli/setup_commands.py src/awf/cli/start_commands.py src/awf/service/doctor/reasons.py
Result: Success, no issues found in 7 source files
```

## Broad Validation

Per the AWF workspace contract, I did not run full repository pytest, full
coverage gates, frontend builds, or CI-equivalent validation. AWF/GitHub own
broad validation, provenance, logs, timeouts, and merge gating after agent
completion.

## Gaps

None for T03. Later setup dry-run, start bootstrap, credential storage, client
configuration, MCP, installer manifest, and installer script behavior remain
intentionally deferred to T04/T05/T06/T08/T09/T11/T12.
