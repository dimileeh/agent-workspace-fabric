# T11 Install Manifest Generator Validation

Plan reference:
`plans/T11_INSTALL_MANIFEST_PLAN.md`

Source implementation contract:

- `docs/awf-plans/ws_c36a07b3ad4e41ffafd88af4.md`
- `plans/AWF_FULL_INSTALLER_FIRST_RUN_SETUP_PLAN.md`
- `TODO/awf-full-installer-first-run-setup-backlog.md`

## Requirement Status

| Requirement | Status | Evidence |
| --- | --- | --- |
| Generate `awf-install-manifest.json` with version, channel, artifact URLs, sha256 values, platform metadata, source metadata, signature-reserved fields, and `generated_at`. | Complete | `scripts/generate_install_manifest.py` builds schema v1 manifests with those fields; `tests/unit/scripts/test_generate_install_manifest.py` asserts manifest structure, platform metadata, signatures, URLs, and deterministic timestamps. |
| Keep manifest output deterministic when inputs and `generated_at` are fixed. | Complete | Generator sorts artifacts by filename and writes `json.dumps(..., indent=2, sort_keys=True) + "\n"`; tests assert exact stable JSON formatting and trailing newline. |
| Read `python-distribution-sha256.txt` without mutating it. | Complete | The generator reads the checksum file only; tests preserve and compare the original checksum file contents across success and failure paths. |
| Require one valid 64-hex sha256 for every distribution artifact and reject missing, duplicate, malformed, stale, unexpected, or content-mismatched entries. | Complete | Generator validates checksum coverage, digest format, duplicate names, stale names, unexpected dist files, and digest/file-content mismatches; tests cover each failure mode. |
| Point artifact URLs at pinned GitHub Release download URLs for the selected tag, never mutable latest, branch/raw, or package-index URLs. | Complete | URLs are constructed as `<repository>/releases/download/<tag>/<filename>` after validating an HTTPS GitHub owner/repo URL and a `v{version}` tag; tests assert pinned URLs and absence of mutable forms. |
| Define stable and prerelease channel semantics, including `auto` channel selection. | Complete | `resolve_channel` maps final versions to `stable`, prerelease/dev versions to `prerelease`, and rejects incompatible explicit channel/version combinations; tests cover stable and prerelease fixtures. |
| Extend the existing publish workflow and preserve the checksum artifact. | Complete | `.github/workflows/publish.yml` now calls `scripts/generate_install_manifest.py` in the existing `build` job after `sha256sum dist/* \| tee artifacts/release/python-distribution-sha256.txt`; upload paths include both the existing checksum file and `awf-install-manifest.json`. |
| Update `RELEASING.md` with manifest inspection and verification guidance. | Complete | `RELEASING.md` documents manifest generation, `jq` inspection, pinned GitHub Release URL shape, channel semantics, checksum verification against both `dist/*` and `python-distribution-sha256.txt`, and reserved `signatures` fields. |
| Preserve H01. | Complete | Docs state GitHub Releases is canonical for v1, `aira.pro` may serve or redirect `install.sh`, v1 installers must verify manifest-pinned `sha256`, and `signatures` fields are reserved for a later signing slice. |
| Keep scope to T11. | Complete | No checked-in `install.sh`, installer smoke, package asset inclusion, or parallel release process was added. |

## Changed Files

- `scripts/generate_install_manifest.py`
- `.github/workflows/publish.yml`
- `RELEASING.md`
- `tests/unit/scripts/test_generate_install_manifest.py`
- `tests/unit/test_publish_workflow_release_artifacts.py`
- `tests/unit/docs/test_release_docs.py`
- `plans/T11_INSTALL_MANIFEST_PLAN.md`
- `plans/T11_INSTALL_MANIFEST_VALIDATION.md`

## Verification Evidence

Red-phase evidence:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py tests/unit/test_publish_workflow_release_artifacts.py tests/unit/docs/test_release_docs.py -q
```

Result before implementation: failed during collection with
`ModuleNotFoundError: No module named 'scripts.generate_install_manifest'`.

Additional red-phase hardening:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py -q
```

Result before checksum-content validation: one failing test,
`test_manifest_rejects_checksum_that_does_not_match_distribution_content`.

Green focused tests:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_generate_install_manifest.py tests/unit/test_publish_workflow_release_artifacts.py tests/unit/docs/test_release_docs.py -q
```

Result after formatter pass: `18 passed in 1.34s`.

Focused lint:

```bash
uv run --python 3.12 --extra dev ruff check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py tests/unit/test_publish_workflow_release_artifacts.py tests/unit/docs/test_release_docs.py
```

Result: `All checks passed!`

Focused format check:

```bash
uv run --python 3.12 --extra dev ruff format --check scripts/generate_install_manifest.py tests/unit/scripts/test_generate_install_manifest.py tests/unit/test_publish_workflow_release_artifacts.py tests/unit/docs/test_release_docs.py
```

Result: `4 files already formatted`.

Focused existing workflow regression:

```bash
uv run --python 3.12 --extra dev pytest tests/unit/test_ci_workflow_full_coverage.py::test_publish_workflow_builds_on_tags_and_uses_trusted_publishing -q
```

Result: `1 passed in 0.41s`.

CLI sanity:

```bash
uv run --python 3.12 --extra dev python scripts/generate_install_manifest.py --help
```

Result: help text printed successfully with exit code 0.

Broad AWF/GitHub validation, full coverage, frontend builds, OpenAPI drift
checks, push, PR creation, and PR monitoring were not run in the agent phase.
Those are managed by AWF/GitHub after agent completion per the workspace
contract.

## Remaining Gaps

None for T11.
