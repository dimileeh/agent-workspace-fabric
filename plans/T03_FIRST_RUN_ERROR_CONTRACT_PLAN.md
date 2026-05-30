# T03 First-Run Error Contract Plan

## Problem Statement

AWF first-run setup/start flows need one shared error contract before the later
setup, start, credential, client, and installer slices add behavior. Operators
must see concise problem/cause/fix/docs output, while automation must receive
JSON-safe payloads with stable reason codes and structured remediation fields.

This slice implements only T03 from
`TODO/awf-full-installer-first-run-setup-backlog.md`, using
`plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md` and
`docs/awf-plans/ws_b459233cc6e6403c935672b8.md` as the source contract.

## Scope

- Add shared first-run rendering models and helpers under `src/awf/host_setup/`.
- Add JSON-safe payloads with stable `reason_code` values and structured
  remediation fields.
- Add catalog entries for the known setup, source-checkout, installer,
  credential, client, and start first-run failures required by T03.
- Wire existing `awf setup` and `awf start` placeholders through the shared
  renderer without implementing real setup/start behavior.
- Redact token-looking strings and provider refs from pretty and JSON output.
- Update generated reason catalog docs.

## Non-Goals

- Do not implement T04 setup dry-run checks.
- Do not implement T05 start bootstrap behavior.
- Do not implement T06 credential storage/backends.
- Do not implement client config writes, MCP setup tools, installer manifests,
  installer scripts, or orchestration behavior.
- Do not reopen locked human decisions H01-H04.
- Do not run AWF/GitHub-owned broad validation in the agent phase.

## Requirements Checklist

- Pretty output renders an operator-facing panel for success, warning, and
  failure payloads.
- Every known first-run failure reason can render problem, cause, fix, docs,
  related command when available, reason code, and safe details.
- JSON output exposes `status`, `command`, `summary`, stable `reason_code`
  where applicable, issue arrays, structured remediation, safe details, and
  next steps.
- Redaction applies before JSON payloads are returned, not only to terminal
  text.
- Token-shaped values, authorization/bearer assignments, URL credentials,
  sensitive detail keys, and provider refs such as `keyring://`, `env://`, and
  `plain-file://` are redacted in both pretty and JSON output.
- New first-run reason codes have non-empty catalog problem/message, likely
  cause, operator fix/action, and docs link.
- Existing setup/start placeholders keep exit code 1 and remain placeholders,
  but emit the shared JSON and pretty shapes.

## Implementation Steps

1. Add failing renderer tests for success, warning, failure, all first-run
   catalog codes, and redaction.
2. Add failing reason catalog tests for the T03 first-run code set and docs
   headings.
3. Update setup/start CLI tests to assert renderer-backed JSON remediation and
   pretty problem/cause/fix/docs labels.
4. Implement `src/awf/host_setup/rendering.py` with strict/frozen Pydantic
   models, reason-catalog adapters, redaction, pretty rendering, JSON rendering,
   and payload constructors.
5. Export the rendering contract and first-run reason-code groups from
   `src/awf/host_setup/__init__.py`.
6. Add first-run catalog entries to `src/awf/service/doctor/reasons.py`.
7. Wire setup/start placeholder commands through the rendering helpers.
8. Regenerate `docs/REASON_CATALOG.md` with the existing generator.
9. Run focused tests and focused lint/type checks for touched Python files.
10. Create `plans/T03_FIRST_RUN_ERROR_CONTRACT_VALIDATION.md` with
    requirement-by-requirement status and focused evidence.

## Focused Verification

Initial expected failing tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_rendering.py -q
uv run --python 3.12 --extra dev pytest tests/unit/service/test_doctor_reasons.py -q
```

Post-implementation targeted tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_host_setup_rendering.py \
  tests/unit/service/test_doctor_reasons.py \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/cli/test_start_commands.py \
  tests/unit/docs/test_catalog_coverage.py \
  -q
```

Focused lint/type checks:

```bash
uv run --python 3.12 --extra dev ruff check \
  src/awf/host_setup \
  src/awf/cli/setup_commands.py \
  src/awf/cli/start_commands.py \
  src/awf/service/doctor/reasons.py \
  tests/unit/service/test_host_setup_rendering.py \
  tests/unit/service/test_doctor_reasons.py \
  tests/unit/cli/test_setup_commands.py \
  tests/unit/cli/test_start_commands.py

uv run --python 3.12 --extra dev mypy \
  src/awf/host_setup \
  src/awf/cli/setup_commands.py \
  src/awf/cli/start_commands.py \
  src/awf/service/doctor/reasons.py
```

Full AWF/GitHub validation, coverage gates, and CI-equivalent suites are left to
AWF after agent completion per the workspace contract.
