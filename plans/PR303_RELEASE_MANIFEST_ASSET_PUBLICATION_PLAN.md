# PR303 Release Manifest Asset Publication Plan

## Context

PR review thread `PRRT_kwDOSJAM6s6FoYch` points out that
`scripts/generate_install_manifest.py` emits GitHub Release download URLs, while
the publish workflow currently uploads distributions, checksums, and the
manifest as GitHub Actions artifacts. Actions artifacts are not available from
`/releases/download/...`, so maintainers need an explicit release-asset
publication step before installer consumers rely on the manifest.

## Scope

- Do not edit `.github/workflows/publish.yml` in this review cycle because it is
  a protected workflow file and this prompt did not grant protected-file
  approval.
- Use the reviewer-provided alternative: document and test the release
  checklist requirement that exact distribution files, checksum file, and
  `awf-install-manifest.json` are uploaded as GitHub Release assets before the
  manifest is consumed.
- Keep the change narrowly scoped to release documentation and its docs
  regression test.

## Steps

1. Add a focused failing docs test that requires release docs to distinguish
   Actions artifacts from GitHub Release assets and to require URL verification
   before manifest consumption.
2. Update `RELEASING.md` to document the required `gh release create` or
   `gh release upload` step and a HEAD-check loop for every manifest URL.
3. Run the targeted docs test only.
4. Record validation evidence and the AWF-managed broad-validation boundary in
   `plans/PR303_RELEASE_MANIFEST_ASSET_PUBLICATION_VALIDATION.md`.
