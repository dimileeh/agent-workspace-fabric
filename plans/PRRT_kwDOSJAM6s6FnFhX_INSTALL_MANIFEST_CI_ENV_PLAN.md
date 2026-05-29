# PRRT_kwDOSJAM6s6FnFhX Install Manifest CI Env Plan

## Problem Statement and Scope

The install manifest generator subprocess test helper inherits the parent
environment when no explicit overrides are supplied. In GitHub Actions, branch
ref variables can make the generator take its intentional skip path, causing
tests that should generate or validate manifests to pass through the wrong code
path.

Scope is limited to the manifest generator unit test helper and documentation
for this review-thread fix.

## Requirements Checklist

- Keep default `_run_generator` calls isolated from ambient GitHub Actions ref
  variables.
- Preserve explicit `env_overrides` behavior so skip-path tests can still set
  `GITHUB_ACTIONS`, `GITHUB_REF_NAME`, and `GITHUB_REF_TYPE`.
- Do not change production manifest skip behavior.
- Use focused validation only; full AWF/GitHub validation is managed after
  agent completion.

## Implementation Steps

1. Add a regression test proving ambient GitHub Actions branch variables in the
   parent pytest process do not affect default generator helper calls.
2. Update `_run_generator` to always pass a copied environment with ambient
   GitHub Actions ref variables removed before applying any explicit overrides.
3. Keep the existing CLI invocation and skip-path assertions unchanged.
4. Record validation evidence in the matching validation document.

## Verification Commands and Pass Criteria

- Repro before fix:
  `GITHUB_ACTIONS=true GITHUB_REF_TYPE=branch GITHUB_REF_NAME=development uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_generator_emits_deterministic_manifest_from_dist_and_checksums tests/unit/scripts/test_generate_install_manifest.py::test_manifest_rejects_malformed_explicit_generated_at -q`
  - Pass criteria before fix: demonstrates the reported failure.
- Focused post-fix:
  `GITHUB_ACTIONS=true GITHUB_REF_TYPE=branch GITHUB_REF_NAME=development uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  - Pass criteria after fix: manifest generator unit tests pass under the CI-like
    branch environment.
- Regression test red/green:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_run_generator_ignores_ambient_github_actions_branch_ref -q`
  - Pass criteria before fix: fails because inherited CI env triggers `SKIP:`.
  - Pass criteria after fix: passes and writes the manifest.
