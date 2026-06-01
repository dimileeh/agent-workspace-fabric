# T13 — Validation: wheel/source packages contain bootstrap and installer assets

Plan reference: `plans/T13_PACKAGE_ASSETS_PLAN.md`
AWF planning artifact: `docs/awf-plans/ws_faabd30e73f4480c97cafa22.md`

## Assumptions / Changes from the plan

- **Build frontend.** The plan proposed in-process `hatchling.build` hooks, but the dev
  `[extra]` does not install `hatchling` (it is only the PEP 517 *backend*). Build-backed tests
  instead drive the repo's standard `uv build` frontend (same backend, offline-capable here),
  cached once per worker in `tests/packaging_build.py`, and skip cleanly via
  `PackageBuildUnavailableError` when the build cannot run. This is functionally equivalent for
  asserting artifact contents and keeps the test honest in CI (which builds release artifacts).
- **Exception naming.** The shared helper's skip signal is `PackageBuildUnavailableError`
  (ruff `N818`, matching repo convention).
- No new `src/awf` production code was required; the only production change is additive
  `pyproject.toml` sdist `include` entries.

## Requirement-by-requirement status

| Req | Status | Evidence |
| --- | --- | --- |
| **AC1** Clean wheel install outside the checkout runs `--help` for `awf`, `setup`, `start`, `init`, `mcp serve` | Complete | `tests/unit/cli/test_clean_install_smoke.py::test_extracted_wheel_help_runs_outside_checkout` (offline-robust: extracted wheel wins over editable `src`, cwd outside checkout, asserts `awf.__file__` under the wheel) and `::test_clean_venv_install_help` (full `python -m venv` + `pip install`; both passed locally). |
| **AC2** `awf start` locates package assets when no source checkout is configured | Complete | `tests/unit/cli/test_start_commands.py::test_service_compose_paths_resolve_from_packaged_root`, `::test_resolve_start_inputs_none_delegates_to_packaged_discovery`, `::test_resolve_start_source_checkout_none_without_stored_source`; `tests/unit/service/test_bootstrap_packaged_assets.py::test_extracted_wheel_resolves_packaged_bootstrap_root` (real extracted wheel resolves as the packaged root with `compose_env_file is None` and `_bootstrap_environment_file == Path(".env")`). |
| **AC3** Source-checkout mode stays explicit and does not mask stale package assets | Complete | `tests/unit/service/test_host_setup_source_assets.py::test_stale_source_metadata_not_masked_by_available_packaged_root` (stale fails `SOURCE_CHECKOUT_ASSETS_STALE` with `fallback_used=False` even when a valid packaged root is resolvable) plus existing `test_stale_source_checkout_metadata_fails_without_package_fallback`; start-level `test_start_commands.py::test_start_stale_stored_metadata_fails_without_fallback`. |
| **AC4** Installer/bootstrap metadata ships in built artifacts | Complete | `pyproject.toml` sdist `include` adds `/packaging/install.sh`, `/scripts/generate_install_manifest.py`, `/RELEASING.md`; verified by `tests/unit/cli/test_packaging.py::test_sdist_includes_installer_release_metadata` (static) and `tests/unit/cli/test_package_build_contents.py::test_sdist_ships_installer_metadata` (built artifact). Wheel bootstrap context + `_is_bootstrap_asset_root` markers + every control-plane COPY input + `METADATA`/entry-point + version-drift guard covered in the same module. Real `uv build` sdist re-inspected: all three present, compose `.env` excluded. |

## Files changed

Production:
- `pyproject.toml` — additive sdist `include` entries for installer/release metadata.

Tests + helpers:
- `tests/packaging_build.py` *(new)* — shared `uv build` helper (wheel+sdist, cached, skip-aware).
- `tests/unit/cli/test_package_build_contents.py` *(new)* — wheel/sdist content + metadata + drift.
- `tests/unit/cli/test_clean_install_smoke.py` *(new)* — clean-install `--help` smoke.
- `tests/unit/cli/test_packaging.py` — sdist installer-metadata static assert.
- `tests/unit/cli/test_start_commands.py` — packaged-asset selection / no-source-checkout tests.
- `tests/unit/service/test_bootstrap_packaged_assets.py` — extracted-wheel packaged-root resolution.
- `tests/unit/service/test_host_setup_source_assets.py` — stale-not-masked-by-packaged-root invariant.

Plan docs:
- `plans/T13_PACKAGE_ASSETS_PLAN.md`, `plans/T13_PACKAGE_ASSETS_VALIDATION.md`.

## Commands run (focused; AWF/CI owns the broad gate)

- `ruff check src/awf tests` → All checks passed.
- `ruff format --check` (changed test files) → already formatted.
- `mypy src/awf` → Success: no issues found in 294 source files.
- `pytest tests/unit/service/test_bootstrap_packaged_assets.py tests/unit/service/test_host_setup_source_assets.py tests/unit/service/test_host_setup_config.py tests/unit/cli/test_packaging.py tests/unit/cli/test_package_build_contents.py tests/unit/cli/test_clean_install_smoke.py tests/unit/cli/test_start_commands.py` → 160 passed.
- `pytest tests/unit/installer` → 112 passed.
- `pytest tests/unit/cli tests/unit/service/test_bootstrap_parts` → 647 passed (no regressions).
- `uv build` → wheel + sdist built; sdist re-inspected for installer metadata + `.env` exclusion.

## Gaps / deferred

None for the T13 acceptance surface. Out of scope by design (owned by their tasks):
T14 E2E install lanes, T15 docs rewrite, T16 release-workflow checks; container-internal
`service/worker.py`/`readiness.py` repo-relative resolution and the demo path (T10/T14).
Per the AWF workspace contract, the whole-repo `--cov` gate and console build run in AWF/GitHub
CI after the agent phase; no new `src/awf` lines were added, so the 99% coverage gate is
unaffected.
