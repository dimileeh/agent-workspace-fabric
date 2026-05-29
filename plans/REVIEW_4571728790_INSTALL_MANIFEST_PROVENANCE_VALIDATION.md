# Review 4571728790 Install Manifest Provenance Validation

Plan reference: `plans/REVIEW_4571728790_INSTALL_MANIFEST_PROVENANCE_PLAN.md`

## Requirement Status

- Complete: `.github/workflows/publish.yml` was not edited; the protected
  workflow file remains unchanged.
- Complete: The CLI now records `GITHUB_SHA` as `source.commit` when running in
  GitHub Actions without an explicit `--commit`.
- Complete: Explicit `--commit` behavior remains first priority, and local
  non-Actions generation still leaves `source.commit` unset unless provided.
- Complete: Added regression coverage for final versions rejected from the
  `prerelease` channel.
- Complete: Validation stayed focused. Full AWF/GitHub validation remains owned
  by AWF after agent completion.

## Evidence

Files changed:

- `scripts/generate_install_manifest.py`
- `tests/unit/scripts/test_generate_install_manifest.py`
- `plans/REVIEW_4571728790_INSTALL_MANIFEST_PROVENANCE_PLAN.md`
- `plans/REVIEW_4571728790_INSTALL_MANIFEST_PROVENANCE_VALIDATION.md`

Commands run:

- Red check:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_generator_records_github_sha_when_actions_tag_ref_omits_commit -q`
  - Result before implementation: failed because `source["commit"]` was `None`.
- Green provenance regression:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_generator_records_github_sha_when_actions_tag_ref_omits_commit -q`
  - Result after implementation: passed, `1 passed`.
- Added channel coverage:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_channel_prerelease_rejects_final_versions -q`
  - Result: passed, `2 passed`.
- Focused manifest tests:
  `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q`
  - Result: passed, `39 passed`.
- Targeted lint:
  `uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py`
  - Result: passed.

## Gaps

None.
