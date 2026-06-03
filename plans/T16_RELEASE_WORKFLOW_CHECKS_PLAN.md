# T16 — Release Workflow Checks (Manifest, Checksums, Installer Smoke) — PLAN

Workspace: `ws_5618ae44f3e84df6bc3795bb` · Target branch: `development` · Auto-merge: on
Backlog: `TODO/awf-full-installer-first-run-setup-backlog.md` → **T16**
Saved contract: `docs/awf-plans/ws_5618ae44f3e84df6bc3795bb.md`
Depends (merged on `development`): T11 (#303), T12 (#318), T13 (#344); H01 locked.

## Problem statement & scope

Extend the existing release pipeline (`.github/workflows/publish.yml` + `RELEASING.md`)
— without building a parallel release process — so the release fails on manifest/checksum
drift and an installer smoke verifies the downloaded artifact checksum *before* any install
mutation. Keep PyPI/uv/pipx manual paths first-class. Touch only release checks + installer
smoke + minimal release docs.

## Requirements checklist

- [ ] AC1 — Release workflow fails if `awf-install-manifest.json` or
  `python-distribution-sha256.txt` drift from built `dist/*` (sha256, filenames,
  channel/version/tag, checksum bytes).
- [ ] AC2 — A workflow job consumes release (or fixture) artifacts and runs
  `packaging/install.sh` so the downloaded artifact checksum is verified before install
  (dry-run; prints "Checksum verified", never installs).
- [ ] AC3 — `RELEASING.md` explains how to verify artifacts manually (drift + installer smoke).
- [ ] AC4 — PyPI Trusted Publishing / `uv tool install` / `pipx install` paths unchanged;
  `publish` keeps its manual `workflow_dispatch` gate.
- [ ] AC5 — Local tests cover expected workflow jobs, artifact names, manifest/checksum
  fixtures, and release smoke command generation.

## Implementation steps (strict TDD; test first, then smallest green)

1. `scripts/check_release_artifacts.py` (new, drift gate) — load published manifest,
   re-derive via `generate_install_manifest.build_manifest(...)` pinning `generated_at` to the
   published value, assert equality with a field-level diff on mismatch; independently re-check
   checksum coverage. `main(argv)` returns 0 / non-zero; map errors via `parser.error`.
   Tests: `tests/unit/scripts/test_check_release_artifacts.py`.
2. `scripts/release_smoke.py` (new, smoke generator/runner) — `build_smoke_manifest`,
   `smoke_invocation`, `main(argv)` with `--dist-dir/--manifest/--smoke-manifest-out/--method
   (repeatable)/--installer/--run`. Tests: `tests/unit/scripts/test_release_smoke.py`.
3. `.github/workflows/publish.yml` — add "Verify release artifact drift" step in `build`;
   new `installer-smoke` job (`needs: build`) consuming the uploaded artifacts. `publish`
   unchanged. Tests: extend `tests/unit/test_publish_workflow_release_artifacts.py`.
4. `RELEASING.md` — add "Verify release artifacts (drift + installer smoke)" subsection.
   Tests: extend `tests/unit/docs/test_release_docs.py`.

## Verification commands & pass criteria

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests scripts
uv run --python 3.12 --extra dev ruff format --check scripts tests/unit/scripts \
  tests/unit/test_publish_workflow_release_artifacts.py tests/unit/docs/test_release_docs.py
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest tests/unit -q \
  -k "release or manifest or installer or workflow or smoke"
python scripts/check_release_artifacts.py --help
python scripts/release_smoke.py --help
bash -n packaging/install.sh
```

Pass = all green; new scripts behavior-tested (scripts/* are outside `--cov=awf`, no
src/awf changes so the 99% gate is unaffected). Workflow-file push permission risk: if AWF's
push is rejected, preserve local work and surface the exact permission failure.

## Assumptions / Changes

- `scripts/` is an importable package (`scripts/__init__.py` exists).
- Repository URL stays `https://github.com/dimileeh/aira-agent-workspace-fabric`.
- Smoke job in `publish.yml` only (keeps `ci.yml` "only python test job" guard intact).
