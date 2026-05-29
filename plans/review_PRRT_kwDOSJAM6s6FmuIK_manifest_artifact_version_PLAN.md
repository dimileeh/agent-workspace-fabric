# Review PRRT_kwDOSJAM6s6FmuIK Manifest Artifact Version Plan

## Problem Statement And Scope

The install manifest generator accepts every supported artifact in `dist/` when the checksum file covers those files. A release checkout with stale `dist/*.whl` or `dist/*.tar.gz` files can therefore generate a manifest for one package/version while including stale prior-version artifacts.

Scope is limited to `scripts/generate_install_manifest.py`, focused unit tests for that script, and this plan/validation record. No workflow, release, or broad validation policy files are in scope.

## Requirements Checklist

- Reject wheel artifacts whose filename distribution or version does not match the requested manifest package/version after normalizing package names consistently across hyphen/underscore spelling.
- Reject sdist artifacts whose filename project or version does not match the requested manifest package/version.
- Keep existing artifact kind, checksum coverage, and checksum content validation behavior intact.
- Preserve deterministic output for valid artifacts.
- Record focused validation evidence only; AWF/GitHub own broad post-agent validation.

## Implementation Steps

1. Add regression tests that create stale same-kind distribution artifacts in `dist/` and assert the generator exits before writing a manifest.
2. Confirm the new regression fails against the current implementation.
3. Add filename metadata parsing/validation for wheel and sdist artifacts in the generator.
4. Re-run the focused manifest-generator tests and any narrow lint needed for the changed files.
5. Create the validation document with requirement-by-requirement status and focused command evidence.

## Verification Commands And Pass Criteria

- Red test: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k stale`
  - Pass criterion before implementation: the new regression fails because stale artifacts are accepted.
- Green tests: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  - Pass criterion after implementation: all manifest-generator tests pass.
- Focused lint: `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  - Pass criterion: no lint findings for changed Python files.

Full AWF/GitHub validation is intentionally not run during this agent phase.
