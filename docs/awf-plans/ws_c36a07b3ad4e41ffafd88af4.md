# T11 Install Manifest Generator And Release Metadata Contract Plan

## Planning Context

This planning artifact is constrained to `docs/awf-plans/ws_c36a07b3ad4e41ffafd88af4.md` by the AWF planning-phase prompt. The implementation phase must still satisfy the repository protocol by creating `plans/T11_INSTALL_MANIFEST_PLAN.md` before coding and `plans/T11_INSTALL_MANIFEST_VALIDATION.md` after implementation.

Source inputs reviewed:

- `plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md`
- `TODO/awf-full-installer-first-run-setup-backlog.md`
- Existing release workflow: `.github/workflows/publish.yml`
- Existing release docs: `RELEASING.md`
- Existing related tests under `tests/unit/scripts`, `tests/unit/docs`, `tests/unit/cli/test_packaging.py`, and `tests/unit/test_ci_workflow_full_coverage.py`

Locked human decision H01 is preserved: GitHub Releases is canonical for v1, `aira.pro` may serve or redirect `install.sh`, v1 requires manifest-pinned `sha256`, and signature fields are reserved but not required for v1.

## Objective

Implement only T11: add a deterministic install manifest generator and release metadata contract for `awf-install-manifest.json`, integrate manifest generation into the existing publish workflow, and update release docs so maintainers can inspect and verify the manifest.

The manifest must describe pinned release artifacts, not mutable `latest` URLs, and must keep the existing wheel/sdist checksum artifact intact.

## Intended Files And Modules To Touch During Implementation

Implementation process artifacts:

- `plans/T11_INSTALL_MANIFEST_PLAN.md`
- `plans/T11_INSTALL_MANIFEST_VALIDATION.md`

Generator and release workflow:

- `scripts/generate_install_manifest.py`
  - New script for generating `awf-install-manifest.json` from a dist directory and checksum file.
  - CLI options should include `--dist-dir`, `--checksums-file`, `--output`, `--version`, `--tag`, `--repository-url`, `--channel`, and deterministic `--generated-at` or `SOURCE_DATE_EPOCH` support.
  - Use only standard-library dependencies where practical so the workflow can run after the existing build setup without new release infrastructure.
- `.github/workflows/publish.yml`
  - Extend the existing build job after `sha256sum dist/* | tee artifacts/release/python-distribution-sha256.txt`.
  - Generate `artifacts/release/awf-install-manifest.json` using the new script.
  - Preserve the existing `python-distribution-sha256.txt` path and existing distribution artifact upload.
  - Upload the manifest as part of the release audit artifacts, either in the existing checksum artifact or a clearly named adjacent artifact. Do not create a parallel workflow.

Release documentation:

- `RELEASING.md`
  - Add a short manifest generation/inspection section.
  - Explain stable and pre-release channel semantics.
  - Explain how to verify manifest hashes against `python-distribution-sha256.txt` and built `dist/*` files.
  - Explain that manifest artifact URLs must be pinned GitHub Release download URLs such as `/releases/download/vX.Y.Z/<filename>`, not `/latest/` or branch/raw URLs.
  - Explain that signature fields are reserved for a future signing slice and are not required for v1.

Tests:

- `tests/unit/scripts/test_generate_install_manifest.py`
  - New focused unit tests for manifest generation, deterministic JSON, pinned URLs, checksum handling, platform metadata, and channel/version selection.
- `tests/unit/test_publish_workflow_full_coverage.py` or a new narrowly named workflow test file such as `tests/unit/test_publish_workflow_release_artifacts.py`
  - Static workflow tests that the publish workflow still creates `python-distribution-sha256.txt` and now creates/uploads `awf-install-manifest.json`.
- `tests/unit/docs/test_release_docs.py` or an extension to an existing docs test if there is a stronger local pattern
  - Documentation drift tests for manifest inspection/verification language and required artifact names.

No `src/awf` runtime modules are expected for T11.

## Manifest Contract

Preferred v1 JSON shape, with exact key names finalized during implementation tests:

```json
{
  "schema_version": 1,
  "package": "agent-workspace-fabric",
  "version": "0.1.0",
  "channel": "stable",
  "generated_at": "2026-05-29T00:00:00Z",
  "source": {
    "repository": "https://github.com/dimileeh/aira-agent-workspace-fabric",
    "tag": "v0.1.0",
    "commit": null
  },
  "artifacts": [
    {
      "name": "agent_workspace_fabric-0.1.0-py3-none-any.whl",
      "kind": "wheel",
      "url": "https://github.com/dimileeh/aira-agent-workspace-fabric/releases/download/v0.1.0/agent_workspace_fabric-0.1.0-py3-none-any.whl",
      "sha256": "<64 hex chars>",
      "platform": {
        "os": "any",
        "arch": "any",
        "python": ">=3.12"
      },
      "signatures": []
    },
    {
      "name": "agent_workspace_fabric-0.1.0.tar.gz",
      "kind": "sdist",
      "url": "https://github.com/dimileeh/aira-agent-workspace-fabric/releases/download/v0.1.0/agent_workspace_fabric-0.1.0.tar.gz",
      "sha256": "<64 hex chars>",
      "platform": {
        "os": "source",
        "arch": "source",
        "python": ">=3.12"
      },
      "signatures": []
    }
  ]
}
```

Contract details:

- `schema_version` starts at `1`.
- `version` is the package version without a leading `v`.
- `source.tag` defaults to `v{version}` and is used in every GitHub Release artifact URL.
- `generated_at` defaults to current UTC time but is injectable for tests; output is stable when this value and inputs are fixed.
- `artifacts` are sorted by filename for deterministic output.
- JSON output uses stable indentation, sorted object keys where feasible, UTF-8, and a trailing newline.
- The generator reads the existing `python-distribution-sha256.txt` format emitted by `sha256sum dist/*` and must not replace or mutate that file.
- Every artifact in `dist/` must have exactly one checksum entry. Missing, duplicate, malformed, or non-64-hex checksum entries fail before writing a manifest.
- Artifact URLs must reject mutable forms including `latest`, branch names, `raw/main`, untagged PyPI URLs, or any URL not tied to the release tag.
- Signature support is represented as reserved empty `signatures` lists or equivalent explicit reserved fields, but no signature verification is required in T11.

## Channel And Version Semantics

Define channel semantics in generator code and `RELEASING.md`:

- `stable`: final package versions intended for default installer resolution. Version strings must not contain pre-release, dev, local, alpha, beta, or rc markers. Example: `0.1.0`.
- `prerelease`: alpha, beta, rc, dev, or otherwise explicitly non-default release candidates. Examples: `0.2.0a1`, `0.2.0b1`, `0.2.0rc1`, `0.2.0.dev1`.
- `auto`: optional generator mode that maps final versions to `stable` and pre/dev versions to `prerelease`.
- A caller may explicitly select `stable` or `prerelease`, but incompatible combinations should fail with a clear CLI error unless the implementation documents a safe override.
- The channel does not change artifact URLs; version and tag pinning control the trust chain.

Use a small deterministic parser for the supported package version forms rather than adding a new runtime dependency solely for release metadata parsing, unless local constraints make that impractical.

## Tests To Write First

Use strict TDD where practical. The implementation phase should create `plans/T11_INSTALL_MANIFEST_PLAN.md`, then write or update the following tests before implementing the generator/workflow/docs changes.

1. `test_manifest_generator_emits_deterministic_manifest_from_dist_and_checksums`
   - Create temp wheel and sdist files with known content.
   - Write a fixture `python-distribution-sha256.txt` matching `sha256sum dist/*` output.
   - Run the generator with fixed `--version`, `--tag`, `--repository-url`, `--channel`, and `--generated-at`.
   - Assert exact JSON-relevant fields, stable artifact order, stable `generated_at`, and trailing newline.

2. `test_manifest_artifact_urls_are_pinned_to_release_tag`
   - Assert generated URLs use `https://github.com/<owner>/<repo>/releases/download/vX.Y.Z/<filename>`.
   - Assert no artifact URL contains `/latest/`, branch/raw paths, or unpinned package-index URLs.

3. `test_manifest_requires_checksums_for_every_distribution_artifact`
   - Missing checksum, duplicate checksum, malformed checksum, or checksum for an absent dist file fails with non-zero CLI behavior or a reason-bearing exception.
   - Existing checksum file contents remain unchanged.

4. `test_manifest_records_platform_metadata_for_wheel_and_sdist`
   - Wheel metadata is platform-independent (`os=any`, `arch=any`, Python `>=3.12`).
   - Sdist/source metadata is distinguishable from the wheel.
   - Unknown file types fail or are ignored only if the behavior is explicitly documented; prefer failing for unexpected `dist/` contents.

5. `test_channel_auto_selects_stable_for_final_versions`
   - Use version fixtures such as `0.1.0`, `1.2.3`, and maybe post-release if supported.
   - Assert `auto` produces `stable` and explicit `stable` accepts final versions.

6. `test_channel_auto_selects_prerelease_for_prerelease_versions`
   - Use fixtures such as `0.2.0a1`, `0.2.0b1`, `0.2.0rc1`, and `0.2.0.dev1`.
   - Assert `auto` produces `prerelease` and explicit incompatible `stable` rejects these versions.

7. `test_publish_workflow_generates_manifest_without_removing_checksum_artifact`
   - Parse `.github/workflows/publish.yml` with `yaml.safe_load`, matching existing workflow-test style.
   - Assert the build job still runs `sha256sum dist/* | tee artifacts/release/python-distribution-sha256.txt`.
   - Assert a manifest generation step invokes `scripts/generate_install_manifest.py`.
   - Assert upload-artifact paths include both `artifacts/release/python-distribution-sha256.txt` and `artifacts/release/awf-install-manifest.json`.

8. `test_releasing_docs_explain_manifest_inspection_and_verification`
   - Assert `RELEASING.md` mentions `awf-install-manifest.json`, `python-distribution-sha256.txt`, pinned GitHub Release URLs, stable/pre-release semantics, and sha256 verification commands or procedure.
   - Assert docs do not recommend mutable `/latest/` manifest artifact URLs.

## Implementation Approach

1. Create `plans/T11_INSTALL_MANIFEST_PLAN.md` from this planning artifact before coding.
2. Add failing generator tests under `tests/unit/scripts/test_generate_install_manifest.py`.
3. Add failing workflow/docs tests for release artifact presence and release-doc contract.
4. Implement `scripts/generate_install_manifest.py` with a small testable core:
   - pure functions for checksum parsing, channel selection, artifact classification, URL construction, manifest assembly, and JSON writing;
   - argparse CLI wrapper returning stable non-zero exits for invalid input;
   - deterministic output when `generated_at` is supplied.
5. Update `.github/workflows/publish.yml` in the existing build job to call the generator after checksums are created and upload the generated manifest with release audit artifacts.
6. Update `RELEASING.md` with manifest generation, inspection, pinned URL, checksum verification, and signature-reserved notes.
7. Run focused tests and lint only for touched files.
8. Create `plans/T11_INSTALL_MANIFEST_VALIDATION.md` comparing the implementation against T11, the source plan, and H01. Record focused command evidence and explicitly note that full AWF/GitHub validation is managed after agent completion.
9. Commit local changes on the current AWF-created branch. Do not switch branches, push, rebase, or run broad validation.

## Validation Commands

Focused validation planned for implementation phase only:

```bash
uv run --python 3.12 --extra dev pytest   tests/unit/scripts/test_generate_install_manifest.py   tests/unit/test_publish_workflow_release_artifacts.py   tests/unit/docs/test_release_docs.py   -q
```

If workflow/docs tests are added to existing files instead of new files, substitute only the touched test files in the command above.

Focused lint for touched Python files:

```bash
uv run --python 3.12 --extra dev ruff check   scripts/generate_install_manifest.py   tests/unit/scripts/test_generate_install_manifest.py   tests/unit/test_publish_workflow_release_artifacts.py   tests/unit/docs/test_release_docs.py
```

Optional focused CLI sanity check for the new script:

```bash
uv run --python 3.12 --extra dev python scripts/generate_install_manifest.py --help
```

Do not run full unit suites, full coverage, frontend builds, OpenAPI drift checks, release builds, Docker builds, or CI-equivalent validation during the agent phase. AWF/GitHub own broad validation, provenance, and merge gating after agent completion.

## Risks And Mitigations

- The current publish workflow uploads Actions artifacts, not necessarily GitHub Release assets. Mitigation: T11 defines and generates manifest metadata using pinned GitHub Release URLs and documents inspection/verification; do not create a separate release process or implement later installer smoke/release gating in this slice.
- Version parsing can grow into full packaging policy. Mitigation: support the concrete final/pre-release forms needed for AWF release tags and tests; avoid a new dependency unless necessary.
- Manifest URL generation may drift from the repository URL in `pyproject.toml`. Mitigation: default `--repository-url` from project metadata where practical and test explicit repository URL input.
- Checksum parsing may mishandle file paths emitted by `sha256sum`. Mitigation: parse the existing `sha256sum dist/*` format conservatively and test path variants with `dist/<name>` and bare filenames if needed.
- Workflow static tests can become brittle if step names change. Mitigation: assert required commands/artifacts in the build job rather than overfitting exact step order, except where order is required after checksum generation.
- Stable/pre-release naming could be confused with the project alpha maturity classifier. Mitigation: document that channel semantics are package-version/channel resolution semantics; product maturity is still communicated elsewhere in release notes/classifiers.

## Assumptions

- T01 dependency is already merged or explicitly satisfied by the operator before T11 implementation begins.
- H01 remains locked and does not require additional approval.
- Python distribution artifacts are the only manifest artifacts for T11; installer script assets are T12 and package asset inclusion is T13.
- The wheel and sdist are platform-independent Python artifacts for v1 metadata purposes.
- The generator may be script-local and does not need to become an `awf` runtime module.
- Target branch management, push, PR creation, broad validation, PR monitoring, and auto-merge remain AWF-owned.

## Explicit Non-Goals

- Do not implement checked-in `install.sh` or checksum-verifying installer behavior; that is T12.
- Do not change package asset inclusion or wheel/sdist contents beyond release manifest generation; that is T13.
- Do not add release workflow installer smoke tests or full manifest/checksum drift gates beyond T11 static artifact checks; that is T16.
- Do not advertise or implement Homebrew, public `curl | bash`, or `aira.pro` hosting behavior beyond preserving the H01 contract.
- Do not create a parallel release workflow or replace PyPI Trusted Publishing.
- Do not alter AWF CLI setup/start/init behavior, credential storage, MCP tools, provider setup, or service bootstrap behavior.
