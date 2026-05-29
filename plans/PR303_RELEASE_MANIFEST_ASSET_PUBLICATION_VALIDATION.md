# PR303 Release Manifest Asset Publication Validation

## Plan Conformance

- Added a focused docs regression test in
  `tests/unit/docs/test_release_docs.py` requiring release docs to distinguish
  Actions artifacts from GitHub Release assets, require `gh release upload`
  coverage, and require URL checks before manifest consumption.
- Updated `RELEASING.md` to document that
  `scripts/generate_install_manifest.py` only emits metadata, that exact
  distributions, `python-distribution-sha256.txt`, and
  `awf-install-manifest.json` must be attached as GitHub Release assets, and
  that every manifest URL must be checked with `curl --fail --head --location`.
- Did not edit `.github/workflows/publish.yml`; it is a protected workflow file
  and this review fix uses the reviewer-provided documentation/enforcement
  alternative.

## Focused Validation

Red check before docs update:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_release_docs.py::test_releasing_docs_require_release_asset_publication_before_manifest_use -q
# failed: missing GitHub Release assets documentation
```

Green check after docs update:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/docs/test_release_docs.py -q
# 2 passed
```

Focused style check for the touched test:

```bash
uv run --python 3.12 --extra dev ruff check tests/unit/docs/test_release_docs.py
# All checks passed
```

## Broad Validation Boundary

Full AWF/GitHub validation, including repository-wide tests, coverage gates,
workflow validation, and CI-equivalent checks, is intentionally left to AWF
after agent completion per the workspace contract.

## Gaps

No implementation gaps found for this review-thread fix.
