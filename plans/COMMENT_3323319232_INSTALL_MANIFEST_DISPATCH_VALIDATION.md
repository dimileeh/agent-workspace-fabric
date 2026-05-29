# Comment 3323319232 Install Manifest Dispatch Validation

Plan reference:
`plans/COMMENT_3323319232_INSTALL_MANIFEST_DISPATCH_PLAN.md`

## Requirement Status

- Manual GitHub Actions `workflow_dispatch` runs from a branch generate the
  manifest instead of returning `SKIP`: Complete.
- Non-dispatch GitHub Actions branch refs continue to skip to avoid producing
  unproven release manifests during ordinary branch CI usage: Complete.
- Existing tag-ref behavior remains unchanged: Complete.
- Stale output removal remains covered for skipped refs: Complete.
- Verification uses focused tests and focused lint only; broad AWF/GitHub
  validation remains owned by AWF after agent completion: Complete.

## Evidence

Files changed:

- `scripts/generate_install_manifest.py`
- `tests/unit/scripts/test_generate_install_manifest.py`
- `plans/COMMENT_3323319232_INSTALL_MANIFEST_DISPATCH_PLAN.md`
- `plans/COMMENT_3323319232_INSTALL_MANIFEST_DISPATCH_VALIDATION.md`

Commands run:

- Red check before implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k "workflow_dispatch or branch_ref"`
  failed with
  `test_manifest_generator_allows_workflow_dispatch_branch_ref` because stdout
  still contained `SKIP: GitHub Actions ref development (branch) is not a release tag`.
- Green regression check after implementation:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q -k "workflow_dispatch or branch_ref"`
  passed: `4 passed, 38 deselected`.
- Focused generator tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  passed: `42 passed`.
- Focused lint:
  `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  passed: `All checks passed!`.

Full AWF/GitHub validation, coverage gates, and CI-equivalent suites were not
run during this agent phase; AWF owns broad validation after completion.
