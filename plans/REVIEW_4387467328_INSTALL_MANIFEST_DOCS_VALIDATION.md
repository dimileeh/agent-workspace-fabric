# Review 4387467328 Install Manifest Docs Validation

Plan reference:
`plans/REVIEW_4387467328_INSTALL_MANIFEST_DOCS_PLAN.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Confirm explicitly supplied `generated_at` values are validated before manifest output. | Complete | Already satisfied before this fix cycle: `scripts/generate_install_manifest.py` routes explicit values through `_validate_generated_at` before assigning `generated_timestamp`; focused generator regression tests pass. |
| Confirm repository URLs with params, query strings, or fragments are rejected before artifact URL assembly. | Complete | Already satisfied before this fix cycle: `_normalize_repository_url` rejects `parsed.params`, `parsed.query`, and `parsed.fragment`; focused generator regression tests pass. |
| Confirm the publish workflow validation table row keeps the shell pipe from breaking the table. | Complete | Already satisfied before this fix cycle: `plans/T11_INSTALL_MANIFEST_VALIDATION.md` uses `sha256sum dist/* \| tee artifacts/release/python-distribution-sha256.txt` in the table cell. |
| Add release docs assertion for `auto` channel semantics. | Complete | `tests/unit/docs/test_release_docs.py` now asserts `"auto" in docs` beside the existing `stable` and `prerelease` checks. |
| Run focused validation and leave broad validation to AWF/GitHub. | Complete | Focused docs and generator regression tests passed. Broad AWF/GitHub validation, coverage gates, frontend builds, pushes, PR creation, and PR monitoring were not run in the agent phase. |

## Verification Evidence

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_release_docs.py -q
```

Result: `1 passed in 0.37s`.

```bash
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py::test_manifest_rejects_malformed_explicit_generated_at tests/unit/scripts/test_generate_install_manifest.py::test_manifest_rejects_repository_urls_with_suffix_components -q
```

Result: `6 passed in 1.04s`.
