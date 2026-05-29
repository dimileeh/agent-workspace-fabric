# PRRT_kwDOSJAM6s6Fit9L Duplicate YAML Keys Plan

## Problem Statement and Scope

The review thread reports that `read_host_setup_config()` parses host setup
YAML with `yaml.safe_load()` before scanning for secrets. PyYAML keeps only the
last value for duplicate mapping keys, so an earlier duplicate
`providers.github.credential_ref` can contain a raw credential while a later
duplicate replaces it with a safe reference before `_ensure_no_secret_payload()`
sees the parsed mapping.

Scope is limited to `src/awf/host_setup/config.py`, focused coverage in
`tests/unit/service/test_host_setup_config.py`, and this plan/validation record.

## Requirements Checklist

- Add a regression proving a duplicate-key host setup config is rejected when an
  earlier duplicate contains a raw credential and a later duplicate contains a
  safe reference.
- Reject duplicate YAML mapping keys during load, before parsed payload secret
  scanning and Pydantic validation.
- Preserve safe diagnostics: do not include raw credential values in error
  messages or `HostSetupConfigError.to_dict()`.
- Preserve existing safe-load behavior for supported YAML and existing corrupt
  config behavior for malformed or recursive payloads.
- Run focused checks only. Full AWF/GitHub validation remains managed by AWF
  after agent completion.

## Implementation Steps

1. Add the focused regression test and confirm it fails against the current
   `yaml.safe_load()` implementation.
2. Add a safe PyYAML loader subclass that rejects duplicate mapping keys with a
   sanitized corrupt-config diagnostic.
3. Route `read_host_setup_config()` through that loader before the existing
   secret scan and validation flow.
4. Run the focused host setup config tests and focused lint for touched files.
5. Write a validation document recording requirement status and command
   evidence.

## Verification Commands and Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "duplicate_yaml_key"`
- `uv run --python 3.12 --extra dev pytest tests/unit/service/test_host_setup_config.py -q -k "host_setup_config"`
- `uv run --python 3.12 --extra dev ruff check src/awf/host_setup/config.py tests/unit/service/test_host_setup_config.py`

Pass criteria: the new focused regression fails before implementation and the
focused checks pass after implementation. Full AWF/GitHub validation remains
managed by AWF after agent completion.
