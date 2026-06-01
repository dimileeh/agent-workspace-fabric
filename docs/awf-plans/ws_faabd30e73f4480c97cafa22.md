# T13 — Ensure wheel/source packages contain bootstrap and installer assets

- Workspace: `ws_faabd30e73f4480c97cafa22`
- Backlog: `TODO/awf-full-installer-first-run-setup-backlog.md` → **T13** (group `packaging`)
- Target branch: `development` (auto-merge enabled)
- Verified-merged dependencies: T02 (host-setup config + source-asset model), T11 (install
  manifest generator), T12 (checked-in `install.sh`), T05 (`awf start` wrapper). T04/T06 are
  **not** dependencies (still active).
- Execution model: Claude Code / `claude-opus-4-8` / `xhigh`.
- Protocol: AGENTS.md + `plans/PLAN_EXECUTION_PROTOCOL.md` (strict TDD; 99% coverage gate).

> This is the AWF-configured planning artifact. During the implementation phase I will also
> create `plans/T13_PACKAGE_ASSETS_PLAN.md` and `plans/T13_PACKAGE_ASSETS_VALIDATION.md` per the
> Plan-Execution Protocol; those are out of scope for this planning step.

---

## 1. Problem statement & scope

Built distributions (`agent-workspace-fabric` wheel + sdist) must let a clean install **outside
the source checkout** run AWF's first-run commands and let `awf start` find the bootstrap assets
(compose files, agent-runtime/control-plane Dockerfiles, migrations, openapi, env example) without
any repo-relative files. The slice also guarantees that explicit/recorded source-checkout mode
stays explicit and is never silently replaced by packaged assets, and that installer-visible
metadata from the checked-in installer work (T11/T12) ships in the built artifacts.

In scope: **package/source asset inclusion and lookup**, and tests that prove it from outside the
checkout. Out of scope: T14 E2E lanes, T15 docs rewrite, T16 release-workflow checks.

## 2. Current state (what already exists — reuse targets, not rebuilds)

Investigation (read-only) established that most of the runtime behavior is already implemented by
T02/T05; T13 is mainly **completeness + verification**:

- `pyproject.toml`
  - `[tool.hatch.build.targets.wheel.force-include]` relocates top-level assets into
    `awf/bootstrap_assets/…` (`.env.example`, `README.md`, `alembic.ini`, `docs`,
    `docker/agent-runtime.Dockerfile`, `docker/compose/local-service.yml`,
    `docker/compose/workspace.base.yml.j2`, `docker/control-plane.Dockerfile`, `migrations`,
    `openapi.json`, `pyproject.toml`, `src`, `uv.lock`); excludes `docker/compose/.env`.
  - `[tool.hatch.build.targets.sdist]` includes the same top-level paths; excludes
    `docker/compose/.env`.
  - Every `docker/control-plane.Dockerfile` COPY input is force-included, so the packaged root is
    a valid Docker build context. `docker/agent-runtime.Dockerfile` has no COPY.
- `src/awf/service/bootstrap.py` — packaged-asset resolution is **done**:
  `PACKAGED_BOOTSTRAP_ASSET_ROOT = Path("bootstrap_assets")`, `_packaged_bootstrap_asset_root()`
  (via `importlib.resources.files("awf")`, rejects non-`Path` zip Traversables),
  `_resolve_bootstrap_asset_root()` (cwd/module discovery → packaged fallback),
  `is_packaged_bootstrap_asset_root()`, `get_bootstrap_asset_root()`, and `_is_bootstrap_asset_root()`
  (requires `docker/agent-runtime.Dockerfile`, `docker/compose/local-service.yml`,
  `docker/control-plane.Dockerfile`, `pyproject.toml`, `src/awf/__init__.py`). The wheel's
  `bootstrap_assets/` satisfies all five markers.
- `src/awf/cli/start_commands.py` + `src/awf/cli/init_ops.py::_resolve_service_compose_paths` —
  `awf start` already selects verified source-checkout assets when given/recorded, otherwise falls
  through to discovery that includes the packaged fallback. Stale/invalid recorded source metadata
  fails loudly (no silent package fallback).
- `src/awf/host_setup/source_assets.py` — source-checkout validation, `VerifiedSourceCheckout`,
  `verified_source_from_metadata()` raising `SOURCE_CHECKOUT_ASSETS_STALE` (never consulting
  packaged assets) — the "no-mask" invariant already holds by construction.
- Installer/manifest contract (T11/T12): `scripts/generate_install_manifest.py`
  (`SCHEMA_VERSION=1`, `CHANNELS={auto,stable,prerelease}`, `DEFAULT_PACKAGE`,
  `DEFAULT_PYTHON_REQUIREMENT`), `packaging/install.sh`, `RELEASING.md`. Version source of truth is
  `pyproject [project].version` (`0.1.0`), duplicated as `awf.__version__` in `src/awf/__init__.py`.
- Existing tests to extend: `tests/unit/cli/test_packaging.py` (static pyproject config asserts +
  control-plane Dockerfile COPY ordering), `tests/unit/service/test_bootstrap_packaged_assets.py`
  (mocked packaged-root edge cases), `tests/unit/service/test_host_setup_source_assets.py`,
  `tests/unit/service/test_host_setup_config.py`, `tests/unit/installer/*`,
  `tests/unit/scripts/test_generate_install_manifest.py`. **No build-backed (archive-content) or
  clean-install smoke test exists yet** — that is the main new test surface.

## 3. Requirements checklist (acceptance criteria → verification)

- [ ] **AC1** Clean wheel install outside the checkout runs `--help` for `awf`, `setup`, `start`,
      `init`, and `mcp serve` without repo-relative files. → clean-venv help smoke test (§6.B).
- [ ] **AC2** `awf start` locates package assets when no source checkout is configured. →
      extracted-wheel packaged-root resolution test (§6.C) + start selection test (§6.E).
- [ ] **AC3** Source-checkout mode remains explicit and does not mask stale package assets. →
      no-mask invariant tests at both the `source_assets` and `awf start` levels (§6.D).
- [ ] **AC4** Installer/bootstrap metadata needed by the package lane is present in built
      artifacts. → sdist installer-metadata inclusion (§5.1) + wheel/sdist content & metadata
      tests (§6.A) + version-drift guard (§6.A).

## 4. Files / modules to touch

Production (minimal):
- `pyproject.toml` — add installer-visible metadata to the **sdist** `include` list (and confirm
  wheel completeness). See §5.1. (No new wheel payload required; packaged root is already valid.)

Tests (the bulk of the work):
- `tests/unit/cli/test_packaging.py` — extend static asserts for the new sdist includes.
- `tests/unit/cli/test_package_build_contents.py` *(new)* — build-backed wheel/sdist content +
  distribution-metadata + version-drift tests (`slow`).
- `tests/unit/cli/test_clean_install_smoke.py` *(new)* — clean-venv help smoke from outside the
  checkout (`slow`).
- `tests/unit/service/test_bootstrap_packaged_assets.py` — extend with an extracted-wheel
  packaged-root resolution test exercised from outside the checkout.
- `tests/unit/service/test_host_setup_source_assets.py` — extend with the "stale not masked by an
  available packaged root" invariant.
- `tests/unit/cli/test_start_commands.py` — extend with the start-level selection/no-mask test
  (recorded stale source fails rather than falling back to packaged discovery; no recorded source
  → packaged discovery is used).

**No new `src/awf/host_setup` production module is planned** (see §8 "Considered and deferred").
The backlog's "Modules touched: src/awf/host_setup" is satisfied by extending host_setup tests,
because the source/package selection contract is already implemented in `source_assets.py` +
`service/bootstrap.py`.

## 5. Implementation steps (smallest green change after each failing test)

### 5.1 Packaging: installer-visible metadata into the sdist
Add to `[tool.hatch.build.targets.sdist].include`:
- `/packaging/install.sh` (T12 checked-in installer)
- `/scripts/generate_install_manifest.py` (T11 manifest generator)
- `/RELEASING.md` (release/manifest contract; also a `SOURCE_CHECKOUT_MARKERS` entry)

Rationale: the sdist is a built artifact of the package lane; shipping the checked-in installer
work + release contract makes the source distribution self-describing for the installer/release
lanes. This is purely additive (new `include` entries) and does not alter the wheel payload or any
runtime path. Wheel payload is left unchanged because the packaged bootstrap root is already valid
and the curl-installer (`install.sh`) is fetched out-of-band, not needed at wheel runtime.

(If review prefers, `install.sh` can also be force-included into the wheel under
`awf/bootstrap_assets/packaging/`; default is **sdist-only** to keep the wheel minimal.)

### 5.2 No runtime/source code changes expected
The packaged-asset lookup, the `awf start` selection, and the no-mask invariant are already
implemented (see §2). If a test in §6 reveals a concrete gap (e.g. the built wheel's
`bootstrap_assets/` fails `_is_bootstrap_asset_root`, or a force-include omission), fix it as the
smallest change in `pyproject.toml` (or the resolver) and capture it as a regression test. Any such
change will be recorded in the implementation `plans/` validation doc.

## 6. Tests to write first (TDD)

General approach for build-backed tests: build in-process via the PEP 517 hooks
`from hatchling.build import build_wheel, build_sdist` into a `tmp_path` (hatchling is already a
build dependency → **no network, no build isolation**), with cwd set to the repo root
(`monkeypatch.chdir`). Mark these `@pytest.mark.slow` and, if needed, raise the per-test timeout
above the 30s default. `uv build` remains the operator's manual cross-check.

### A. Package content tests (wheel + sdist) — `test_package_build_contents.py` (`slow`)
- Build a wheel; open it as a zip; assert presence of:
  `awf/bootstrap_assets/docker/compose/local-service.yml`, `.../workspace.base.yml.j2`,
  `.../docker/agent-runtime.Dockerfile`, `.../docker/control-plane.Dockerfile`,
  `awf/bootstrap_assets/migrations/`, `.../alembic.ini`, `.../openapi.json`, `.../.env.example`,
  `.../pyproject.toml`, `.../uv.lock`, `.../src/awf/__init__.py`, `.../docs/`.
- Assert the five `_is_bootstrap_asset_root` markers exist under `awf/bootstrap_assets/`, and that
  **every** `control-plane.Dockerfile` COPY input is present (guards the in-container build).
- Assert `docker/compose/.env` is **absent** from the wheel.
- Distribution metadata: parse `*.dist-info/METADATA` → `Name == agent-workspace-fabric`,
  `Version == pyproject[project].version`; assert the `awf` console entry point exists in
  `entry_points.txt` (`awf = awf.cli.main:app`).
- Build an sdist; assert it contains top-level `docker/…`, `migrations/`, `src/`, `openapi.json`,
  **and** the newly-added `packaging/install.sh`, `scripts/generate_install_manifest.py`,
  `RELEASING.md`; assert `docker/compose/.env` is absent.
- **Version-drift guard:** `awf.__version__ == pyproject[project].version` (and, when resolvable,
  `importlib.metadata.version("agent-workspace-fabric")`). Protects the installer's post-install
  version check.

### B. Clean-install help smoke from outside the checkout — `test_clean_install_smoke.py` (`slow`)
- Build the wheel (§A). Create a venv with `python -m venv --system-site-packages <tmp_venv>` (so
  runtime deps resolve from the parent dev env), then `pip install --no-deps --force-reinstall
  <wheel>` into it (no network; `awf` resolves from the venv install, shadowing the editable one).
- From a cwd **outside** the checkout (a separate `tmp_path`), run `<venv>/bin/awf --help`,
  `awf setup --help`, `awf start --help`, `awf init --help`, `awf mcp serve --help` as subprocesses;
  assert exit 0 and a stable fragment for each. (`awf mcp serve --help` verified, not `serve`.)
- Skip with a clear reason if `python -m venv`/`pip` are unavailable, to keep the unit suite robust;
  the full lane is owned by T14.
- Risk/mitigation for editable shadowing is noted in §8.

### C. Packaged-root resolution from an extracted wheel — extend `test_bootstrap_packaged_assets.py`
- Build a wheel, extract `awf/bootstrap_assets/` into `tmp_path`, and drive
  `bootstrap._packaged_bootstrap_asset_root` / `is_packaged_bootstrap_asset_root` /
  `get_bootstrap_asset_root` by monkeypatching `bootstrap.files` to a fake that
  `joinpath("bootstrap_assets")` → the extracted `Path`. With cwd outside any checkout
  (`monkeypatch.chdir(tmp_path)`), assert the packaged root resolves and passes
  `_is_bootstrap_asset_root`, and that `_resolve_bootstrap_assets(LOCAL_SERVICE_COMPOSE_FILE,
  require_agent_runtime=True)` returns compose/agent-runtime paths under the extracted root with
  `compose_env_file is None` and `_bootstrap_environment_file(...) == Path(".env")`.

### D. "Stale source not masked by available packaged assets" — extend source/start tests
- `test_host_setup_source_assets.py`: build/point a valid packaged root available to the process,
  record valid source metadata, then invalidate the source checkout; assert
  `verified_source_from_metadata(...)` raises `SOURCE_CHECKOUT_ASSETS_STALE` and returns **no**
  packaged fallback even though packaged assets are present.
- `test_start_commands.py`: with `read_host_setup_config()` returning recorded (now-stale) source
  metadata, assert `_resolve_start_source_checkout(None)` raises `SourceCheckoutError` (→
  `SOURCE_CHECKOUT_ASSETS_STALE`) instead of falling back to packaged discovery.

### E. Start uses packaged assets when no source checkout configured — extend `test_start_commands.py`
- With config carrying no `source_checkout`, assert `_resolve_start_source_checkout(None) is None`
  and `_resolve_start_bootstrap_inputs(None)` yields compose paths sourced from
  `get_bootstrap_asset_root()` (packaged root), using a fake/stub packaged root via `init_ops` so
  the test does not depend on the live checkout. (Reuse existing start-command test fakes.)

## 7. Validation commands (focused; AWF/CI owns the broad gate)

```bash
uv run --python 3.12 --extra dev ruff check src/awf tests
uv run --python 3.12 --extra dev mypy src/awf
uv run --python 3.12 --extra dev pytest \
  tests/unit/service/test_bootstrap_packaged_assets.py \
  tests/unit/service/test_host_setup_source_assets.py \
  tests/unit/service/test_host_setup_config.py -q
uv run --python 3.12 --extra dev pytest tests/unit/cli/test_packaging.py \
  tests/unit/cli/test_package_build_contents.py \
  tests/unit/cli/test_clean_install_smoke.py \
  tests/unit/cli/test_start_commands.py -q
uv run --python 3.12 --extra dev pytest tests/unit/installer -q
uv build      # operator-sanctioned manual cross-check of the built artifacts
```

Per the AWF workspace contract, the full `.awf/workspace.yml` suite, whole-repo coverage gate, and
console build are **not** run in the agent phase — AWF/GitHub CI own them after completion.

## 8. Risks, assumptions, non-goals

Assumptions:
- `hatchling` PEP 517 hooks build offline in this environment (it is a declared build dep); version
  is static in `pyproject` (no VCS plugin), so no git state is needed to build.
- The dev venv has all runtime deps installed, enabling the `--system-site-packages` + `--no-deps`
  clean-install smoke without network.
- `agent-workspace-fabric` builds a pure-py3 wheel (`py3-none-any`); the `awf` console script is the
  only entry point.

Risks & mitigations:
- **Build-test latency / 30s timeout.** Building copies `src`, `docs`, `migrations` into the wheel.
  Mark `slow`; raise the per-test timeout if needed; keep build-backed tests few and focused.
- **Editable-install shadowing in the smoke test.** A `.pth`/meta-path editable finder for `awf`
  could win over the venv install. Mitigation: install into the venv with `--force-reinstall
  --no-deps` and invoke the venv's own `<venv>/bin/awf`; assert the resolved `awf.__file__` is under
  the venv before asserting help output. If precedence proves unreliable, fall back to asserting
  provenance via an explicit constructed `sys.path` (extracted wheel dir first).
- **Coordination overlap (`STALE_OVERLAP`).** Active workspaces `ws_0ea630b2…` (owns
  `pyproject.toml`, `host_setup/config.py`, `host_setup/__init__.py`,
  `tests/unit/service/test_host_setup_config.py`) and `ws_4c144afc…` (owns
  `host_setup/__init__.py`) overlap. Mitigations: keep `pyproject.toml` edits **purely additive**
  (three new sdist `include` lines); do **not** edit `host_setup/__init__.py`, `host_setup/config.py`,
  or `test_host_setup_config.py`; if a new symbol were ever needed it would be imported by full
  module path, not re-exported via `__init__`. If those land first, expect an AWF rebase/revalidate.
- **No new abstraction.** Adding a selection façade risks two sources of truth and layer inversion.

Considered and deferred (explicit non-goals):
- A new `src/awf/host_setup/package_assets.py` selection façade — **not added**. The
  source/package selection and the no-mask invariant are already implemented and testable from
  outside the checkout; AGENTS.md prefers existing patterns over new abstractions, and it would
  widen the `host_setup/__init__.py` coordination overlap.
- Fixing `service/worker.py` / `service/controls_helpers.py` `__file__.parents[3]` workspace-template
  resolution and `service/readiness.py` `DEFAULT_DEMO_PATH` (`examples/awf-core-demo`, not
  force-included). These run **inside** the control-plane container (built from
  `control-plane.Dockerfile`, WORKDIR `/app`, which COPYs the repo layout) or belong to the no-token
  smoke proof (T10) / E2E (T14). They are not exercised by T13's acceptance (`--help` + packaged
  `awf start` asset location) and are flagged here for T10/T14 rather than changed in this slice.
- T14 E2E install lanes, T15 docs rewrite, T16 release-workflow checks — owned by their tasks.
