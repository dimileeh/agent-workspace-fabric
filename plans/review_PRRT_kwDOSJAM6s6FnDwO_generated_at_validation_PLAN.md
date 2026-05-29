# Review PRRT_kwDOSJAM6s6FnDwO Generated-At Validation Plan

## Problem Statement and Scope

An unresolved review thread reports that `scripts/generate_install_manifest.py`
accepts an explicitly supplied `--generated-at` value without validating that it
matches the manifest timestamp contract. Scope is limited to the install
manifest generator, focused unit tests for that script, and this plan/validation
record.

## Requirements

- Reject malformed explicit `generated_at` values before writing a manifest.
- Preserve the existing default timestamp behavior when `generated_at` is not
  supplied.
- Keep accepted explicit timestamps in canonical UTC `YYYY-MM-DDTHH:MM:SSZ`
  format.
- Add focused regression coverage for the invalid explicit timestamp case.
- Run only targeted validation; full AWF/GitHub validation remains owned by AWF
  after agent completion.

## Implementation Steps

1. Add a failing focused regression test that passes malformed `--generated-at`
   and expects a parser error with no manifest output.
2. Run the new targeted test to confirm it fails against the current code.
3. Add a small validation helper in `scripts/generate_install_manifest.py` and
   call it only when an explicit `generated_at` is provided.
4. Run the focused regression, the script test file, and focused ruff check.
5. Create the validation record with requirement-by-requirement evidence.

## Verification Commands

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_rejects_malformed_explicit_generated_at -q`
- Focused tests: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
- Focused lint: `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
