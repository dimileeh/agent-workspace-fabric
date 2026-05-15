# Compound Setup Dependency Context Plan

## Problem Statement And Scope

Address PR thread `PRRT_kwDOSJAM6s6CM_D2` against `src/awf/runtime/validation.py`.
The setup dependency network classifier currently allows compound setup commands
such as `pip install ... && ./bootstrap` to classify later non-dependency output
as dependency fetch failures when that output contains generic context words such
as `fetch`.

Scope is limited to classifier evidence for compound setup commands and focused
unit coverage.

## Requirements Checklist

- Add a regression test showing a chained bootstrap failure with generic
  `fetch` wording is not classified as `SETUP_DEPENDENCY_NETWORK_FAILURE`.
- Preserve classification for chained commands when output includes
  package/index-specific dependency evidence.
- Keep existing single-command dependency setup classification behavior intact.
- Run the narrow relevant unit tests and record the result.

## Implementation Steps

1. Add the failing regression test under `tests/unit/runtime/test_validation.py`.
2. Tighten compound-command context in `src/awf/runtime/validation.py` to require
   package/index-specific evidence instead of generic dependency-context words.
3. Run the narrow test selection that covers setup dependency classification.
4. Create the validation document with requirement status and evidence.

## Verification Commands And Pass Criteria

- `uv run --python 3.12 --extra dev pytest tests/unit/runtime/test_validation.py -q -k setup_dependency_network_classifier`

Pass criteria: all selected setup dependency classifier tests pass, including the
new regression.
