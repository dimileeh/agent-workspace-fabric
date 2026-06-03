# T16 — Release Workflow Checks — VALIDATION

Plan reference: `plans/T16_RELEASE_WORKFLOW_CHECKS_PLAN.md`
Saved contract: `docs/awf-plans/ws_5618ae44f3e84df6bc3795bb.md`

## Requirement-by-requirement status

| AC | Requirement | Status | Evidence |
| --- | --- | --- | --- |
| AC1 | Release workflow fails on manifest/checksum drift | Complete | `scripts/check_release_artifacts.py` (new); `build` job step "Verify release artifact drift" in `.github/workflows/publish.yml`; `tests/unit/scripts/test_check_release_artifacts.py` (9 tests); `tests/unit/test_publish_workflow_release_artifacts.py::test_publish_workflow_build_verifies_release_artifact_drift` |
| AC2 | Installer smoke verifies downloaded artifact checksum before install | Complete | `scripts/release_smoke.py` (new); new `installer-smoke` job (`needs: build`) consuming `python-distributions` + `python-distribution-checksums`; `tests/unit/scripts/test_release_smoke.py` (incl. checksum-mismatch-before-install); `..._has_installer_smoke_job_consuming_release_artifacts` |
| AC3 | Publish docs explain manual artifact verification | Complete | `RELEASING.md` → "Verify release artifacts (drift + installer smoke)"; `tests/unit/docs/test_release_docs.py::test_releasing_docs_explain_manual_artifact_drift_and_installer_smoke` |
| AC4 | PyPI/uv/pipx manual paths first-class; `publish` keeps manual gate | Complete | `publish` job unchanged (`workflow_dispatch` + `inputs.publish_target != 'none'` + Trusted Publishing); `..._publish_job_keeps_manual_trusted_publishing_gate`; existing `test_publish_workflow_builds_on_tags_and_uses_trusted_publishing` still passes |
| AC5 | Local tests cover jobs, artifact names, fixtures, smoke command generation | Complete | New script tests + extended workflow/docs tests; 41 focused tests pass |

All requirements **Complete**. No `src/awf` production code changed, so the 99%
coverage gate is unaffected; `scripts/*` are outside `--cov=awf` but are behavior-tested
(no coverage padding).

## Files changed

Production / pipeline:
- `scripts/check_release_artifacts.py` *(new)* — drift gate; reuses
  `generate_install_manifest.build_manifest` / `_parse_checksums` /
  `_validate_checksum_coverage`.
- `scripts/release_smoke.py` *(new)* — `build_smoke_manifest`, `smoke_invocation`,
  `main(--run)`.
- `.github/workflows/publish.yml` — drift-check step in `build` (exports
  `AWF_RELEASE_VERSION`/`AWF_RELEASE_TAG` to `$GITHUB_ENV`); new `installer-smoke` job;
  `publish` unchanged.
- `RELEASING.md` — manual verification subsection.

Tests:
- `tests/unit/scripts/test_check_release_artifacts.py` *(new)*.
- `tests/unit/scripts/test_release_smoke.py` *(new)*.
- `tests/unit/test_publish_workflow_release_artifacts.py` — extended (3 new tests).
- `tests/unit/docs/test_release_docs.py` — extended (1 new test).

## Commands run (evidence)

- `uv run --python 3.12 --extra dev pytest tests/unit -q -k "release or manifest or installer or workflow or smoke"` → **707 passed**.
- `uv run --python 3.12 --extra dev pytest tests/unit/scripts/test_check_release_artifacts.py tests/unit/scripts/test_release_smoke.py tests/unit/test_publish_workflow_release_artifacts.py tests/unit/test_ci_workflow_full_coverage.py tests/unit/docs/test_release_docs.py -q` → **41 passed**.
- `uv run --python 3.12 --extra dev ruff check src/awf tests scripts` → **All checks passed**.
- `uv run --python 3.12 --extra dev ruff format --check scripts tests/unit/scripts ...` → **all formatted**.
- `python scripts/check_release_artifacts.py --help` / `python scripts/release_smoke.py --help` → OK.
- `bash -n packaging/install.sh` → OK (installer untouched).
- Manual end-to-end: generated a manifest from fixture dist, ran the drift gate (pass),
  ran `release_smoke.py --run` (printed `Checksum verified`, no install), then tampered the
  manifest `sha256` and confirmed the drift gate fails (exit 2) with a field-level diff.

## Notes / boundaries honored

- No T14 (E2E harnesses) and no T15 (README/quickstart rewrite); only `RELEASING.md` touched.
- Smoke job kept in `publish.yml` (not `ci.yml`), so the `ci.yml` "only python test job"
  guard (`test_full_coverage_is_the_only_python_test_job`) still passes.
- Workflow-file push permission risk: if AWF's push of `.github/workflows/publish.yml` is
  rejected (token lacks `workflows` permission), the local work is preserved on-branch and the
  exact failure should be surfaced — the workflow change must not be silently dropped.
- Broad AWF/GitHub validation (full coverage gate, OpenAPI drift, console build) is owned by
  AWF/CI after the agent phase and was not run here per the workspace contract.
