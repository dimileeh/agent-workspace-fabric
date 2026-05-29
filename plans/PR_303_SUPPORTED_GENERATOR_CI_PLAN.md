# PR 303 Supported Generator CI Plan

## Problem Statement And Scope

PR #303 fails CI in
`tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators`.
The focused repro shows the cleanup guard rejects
`scripts/generate_install_manifest.py`, even though the release manifest slice
added it as a supported release generator and existing release docs/tests refer
to it.

Scope is limited to the stale supported-generator assertion and this
plan/validation record. Do not edit workflow, quality-gate, or protected
configuration files.

## Requirements Checklist

- Reproduce the reported pytest failure before changing code.
- Do not disable, skip, or weaken the cleanup guard.
- Keep `scripts/` restricted to explicitly supported generator scripts.
- Recognize the install manifest generator as supported because it is now part
  of the release artifact contract.
- Run focused validation only; full AWF/GitHub validation remains owned by AWF
  after agent completion.
- Commit the local fix without pushing or switching branches.

## Implementation Steps

1. Update the cleanup docs test to use an explicit supported generator set that
   includes `generate_install_manifest.py`, `generate_openapi.py`, and
   `generate_reason_catalog.py`.
2. Keep the assertion exact so unrelated scripts still fail the guard.
3. Re-run the reported pytest node.
4. Run a focused lint check for the edited test file.
5. Write the validation document with evidence.

## Verification Commands And Pass Criteria

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_api_surface_cleanup_docs.py::test_scripts_directory_contains_only_supported_generators -q
```

Pass criterion: the reported node passes.

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_api_surface_cleanup_docs.py
```

Pass criterion: the edited test file passes lint.

Full coverage and CI-equivalent validation are intentionally not run locally;
AWF/GitHub own those broad checks after agent completion.
