# T11 Install Manifest Generator Plan

## Problem Statement And Scope

Implement T11 from the first-run installer backlog: add a deterministic
`awf-install-manifest.json` generator and release metadata contract for AWF
Python release artifacts.

Scope is limited to:

- a new manifest generator script;
- focused generator, workflow, and release documentation tests;
- extending the existing publish workflow to emit the manifest beside the
  existing checksum artifact;
- updating `RELEASING.md` with manifest inspection and verification guidance.

Out of scope:

- checked-in `install.sh`;
- installer smoke tests;
- package asset inclusion changes;
- new release workflow jobs or a parallel release process;
- signature verification implementation.

Locked human decision H01 is preserved: GitHub Releases is canonical for v1,
`aira.pro` may serve or redirect `install.sh`, v1 requires manifest-pinned
`sha256`, and signature fields are reserved but not required for v1.

## Requirements Checklist

- Generate `awf-install-manifest.json` with version, channel, artifact URLs,
  sha256 values, platform metadata, source metadata, signature-reserved fields,
  and `generated_at`.
- Keep manifest output deterministic when inputs and `generated_at` are fixed.
- Read the existing `python-distribution-sha256.txt` format without mutating
  the checksum artifact.
- Require one valid 64-hex sha256 for every distribution artifact and reject
  missing, duplicate, malformed, stale, or unexpected dist entries.
- Point artifact URLs at pinned GitHub Release download URLs for the selected
  tag, never mutable `/latest/`, branch, raw, or package-index URLs.
- Define stable and prerelease channel semantics, including `auto` channel
  selection from package versions.
- Extend `.github/workflows/publish.yml` rather than creating a separate release
  workflow, and preserve the existing wheel/sdist checksum artifact.
- Update `RELEASING.md` so maintainers can inspect the manifest and verify its
  sha256 values against both `dist/*` and `python-distribution-sha256.txt`.

## Implementation Steps

1. Add failing unit tests for the manifest generator under
   `tests/unit/scripts/test_generate_install_manifest.py`.
2. Add failing static workflow tests proving the publish workflow still creates
   `python-distribution-sha256.txt` and now creates/uploads
   `awf-install-manifest.json`.
3. Add failing release docs tests proving `RELEASING.md` documents manifest
   inspection, stable/prerelease semantics, pinned GitHub Release URLs,
   sha256 verification, and reserved signature fields.
4. Implement `scripts/generate_install_manifest.py` with standard-library-only
   helpers for checksum parsing, artifact classification, channel selection,
   URL construction, manifest assembly, and CLI output.
5. Patch `.github/workflows/publish.yml` to invoke the generator after the
   checksum file is created and upload the manifest with release audit
   artifacts.
6. Patch `RELEASING.md` with manifest generation, inspection, and verification
   instructions.
7. Run focused tests and lint for only the changed files.
8. Create `plans/T11_INSTALL_MANIFEST_VALIDATION.md` with requirement status,
   changed files, focused command evidence, and a note that broad AWF/GitHub
   validation is managed after agent completion.
9. Commit the completed local changes on the current AWF-managed branch.

## Verification Commands And Pass Criteria

Focused tests:

```bash
uv run --python 3.12 --extra dev pytest \
  tests/unit/scripts/test_generate_install_manifest.py \
  tests/unit/test_publish_workflow_release_artifacts.py \
  tests/unit/docs/test_release_docs.py \
  -q
```

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check \
  scripts/generate_install_manifest.py \
  tests/unit/scripts/test_generate_install_manifest.py \
  tests/unit/test_publish_workflow_release_artifacts.py \
  tests/unit/docs/test_release_docs.py
```

Optional CLI sanity check:

```bash
uv run --python 3.12 --extra dev python scripts/generate_install_manifest.py --help
```

Pass criteria:

- The focused tests pass.
- Ruff reports no issues in changed Python files.
- The generator exits successfully for valid deterministic fixtures and exits
  non-zero with clear errors for invalid checksum/channel inputs.
- The publish workflow retains `python-distribution-sha256.txt` and uploads
  `awf-install-manifest.json`.
- Release docs explain manifest inspection and sha256 verification without
  recommending mutable `/latest/` artifact URLs.

Full repository validation, coverage, frontend builds, OpenAPI drift checks,
push, PR creation, and PR monitoring are intentionally left to AWF/GitHub after
agent completion.
