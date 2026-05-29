# PRRT_kwDOSJAM6s6FnFhX Install Manifest CI Env Validation

Plan reference: `PRRT_kwDOSJAM6s6FnFhX_INSTALL_MANIFEST_CI_ENV_PLAN.md`

## Requirement Status

- Complete: Default `_run_generator` calls are isolated from ambient GitHub
  Actions ref variables by removing `GITHUB_ACTIONS`, `GITHUB_REF_NAME`, and
  `GITHUB_REF_TYPE` from the copied subprocess environment.
- Complete: Explicit `env_overrides` are applied after ambient cleanup, so
  skip-path tests still opt into GitHub Actions branch or tag refs.
- Complete: Production manifest skip behavior is unchanged; only
  `tests/unit/scripts/test_generate_install_manifest.py` was edited.
- Complete: Validation was focused to the changed test surface. Full AWF/GitHub
  validation is managed by AWF after agent completion.

## Evidence

Files changed:

- `tests/unit/scripts/test_generate_install_manifest.py`
- `plans/PRRT_kwDOSJAM6s6FnFhX_INSTALL_MANIFEST_CI_ENV_PLAN.md`
- `plans/PRRT_kwDOSJAM6s6FnFhX_INSTALL_MANIFEST_CI_ENV_VALIDATION.md`

Commands run:

- Before fix:
  `GITHUB_ACTIONS=true GITHUB_REF_TYPE=branch GITHUB_REF_NAME=development uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_generator_emits_deterministic_manifest_from_dist_and_checksums tests/unit/scripts/test_generate_install_manifest.py::test_manifest_rejects_malformed_explicit_generated_at -q`
  - Result: failed with missing manifest output and unexpected return code `0`
    from the skip path.
- Regression red step:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_run_generator_ignores_ambient_github_actions_branch_ref -q`
  - Result before helper fix: failed because stdout contained `SKIP:`.
- Regression green step:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_run_generator_ignores_ambient_github_actions_branch_ref -q`
  - Result after helper fix: passed, `1 passed`.
- Focused CI-like file run:
  `GITHUB_ACTIONS=true GITHUB_REF_TYPE=branch GITHUB_REF_NAME=development uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  - Result: passed, `34 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check tests/unit/scripts/test_generate_install_manifest.py`
  - Result: passed.

## Gaps

None.
