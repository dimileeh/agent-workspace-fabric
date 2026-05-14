# Review Thread PRRT_kwDOSJAM6s6CNxRI Plan

## Problem Statement And Scope

The setup dependency network classifier currently accepts `_SETUP_PACKAGE_SPEC_RE`
matches as dependency evidence for unknown setup wrappers. The single `=`
operator can match ordinary environment or configuration assignments such as
`CONFIG_URL=https://...`, causing unrelated bootstrap/config service network
failures to consume the setup dependency retry budget and report
`SETUP_DEPENDENCY_NETWORK_FAILURE`.

Scope is limited to `src/awf/runtime/validation.py` and focused regression
coverage in `tests/unit/runtime/test_validation.py`.

## Requirements Checklist

- Unknown setup wrappers must not classify assignment-like `KEY=value` output as
  package evidence.
- Known dependency outputs using existing supported package/version syntax must
  continue to classify.
- Add a regression test that fails before the code change.
- Keep the change narrow and preserve existing redaction and host extraction
  behavior.

## Implementation Steps

1. Add a unit regression covering `./bootstrap` output containing
   `CONFIG_URL=https://api.internal.example/config: connection timed out`.
2. Confirm the new regression fails against the current implementation.
3. Tighten setup dependency package extraction so assignment-like single-`=`
   matches are ignored as package evidence.
4. Run the focused runtime validation tests that cover setup dependency
   classification.
5. Document verification in the matching validation file.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q`
  passes.
- `uv run --python 3.12 --extra dev ruff check src/awf/runtime/validation.py tests/unit/runtime/test_validation.py`
  passes.
