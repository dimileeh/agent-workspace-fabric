# PRRT_kwDOSJAM6s6Fmwmo Branch Dispatch Manifest Validation

Plan reference: `plans/PRRT_kwDOSJAM6s6Fmwmo_BRANCH_DISPATCH_MANIFEST_PLAN.md`

## Requirement Status

- Complete: Skip manifest generation in GitHub Actions when the current ref is a
  branch, including branch names that start with `v`.
  - Evidence: `scripts/generate_install_manifest.py` now skips when
    `GITHUB_ACTIONS=true` and the ref is not the exact release tag; focused tests
    cover `development` and `vnext` branch refs.
- Complete: Remove any pre-existing manifest output when skipping so stale files
  cannot be uploaded.
  - Evidence: the branch-ref regression pre-creates
    `awf-install-manifest.json` and asserts it is absent after the skip.
- Complete: Continue generating manifests for local invocations and valid GitHub
  Actions tag refs.
  - Evidence: existing local-generator tests still pass, and the new tag-ref
    regression covers `GITHUB_REF_TYPE=tag` with `GITHUB_REF_NAME=v0.1.0`.
- Complete: Preserve existing manifest validation behavior for real release tags.
  - Evidence: the full focused manifest-generator test file passes.
- Complete: Record focused validation evidence only.
  - Evidence: no full suite, full coverage, frontend build, OpenAPI drift check,
    or CI-equivalent validation was run in this agent phase.

## Commands Run

- Red check: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k github_actions_branch`
  - Before implementation: failed because branch refs still wrote
    `awf-install-manifest.json`.
- Green branch regression: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k github_actions_branch`
  - Result: `2 passed, 25 deselected`.
- Focused manifest tests: `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  - Result: `27 passed`.
- Focused lint: `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  - Result: `All checks passed!`

## Remaining Gaps

None. Full AWF/GitHub validation, provenance, and merge gating are managed after
agent completion.
