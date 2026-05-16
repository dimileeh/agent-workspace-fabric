# Review Thread PRRT_kwDOSJAM6s6CL2CV Plan

## Problem statement and scope

The setup dependency network classifier extracts `classification.package` from
`raw_context`, which starts with the setup command. When the command contains a
credential-bearing package index URL, the package regex can capture the URL
credential substring before reaching the actual failed package in output. Scope
is limited to preventing secret-bearing URL credentials from becoming package
metadata while preserving existing retry classification behavior.

## Requirements checklist

- Add a regression test that fails when a credential-bearing index URL is
  captured as the package.
- Ensure setup dependency package extraction does not emit URL credentials or
  known tokens.
- Preserve package extraction for the real dependency in the setup output or
  non-secret command text.
- Keep host extraction and diagnostic redaction behavior unchanged.
- Run the narrow affected tests and relevant lint for touched files.

## Implementation steps

1. Add a focused unit test in `tests/unit/runtime/test_validation.py` for an
   index URL containing a token before stderr references the real package.
2. Confirm the new test fails against the current implementation.
3. Sanitize the package extraction context before applying package regexes.
4. Re-run the focused test module or selected tests until green.
5. Create a validation report with requirement status and command evidence.

## Verification commands and pass criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
