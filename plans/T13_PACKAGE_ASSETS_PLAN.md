# T13 — Ensure wheel/source packages contain bootstrap and installer assets

Backlog: `TODO/awf-full-installer-first-run-setup-backlog.md` → **T13** (group `packaging`).
AWF planning artifact (full design): `docs/awf-plans/ws_faabd30e73f4480c97cafa22.md`.
Protocol: `plans/PLAN_EXECUTION_PROTOCOL.md` (strict TDD; 99% coverage gate owned by CI).

## 1. Problem statement & scope

Built distributions (`agent-workspace-fabric` wheel + sdist) must let a clean install
**outside the source checkout** run AWF's first-run commands (`awf`, `setup`, `start`,
`init`, `mcp serve` `--help`) and let `awf start` find packaged bootstrap assets (compose
files, agent-runtime/control-plane Dockerfiles, migrations, openapi, env example) without
any repo-relative files. Explicit/recorded source-checkout mode must stay explicit and is
never silently replaced by packaged assets. Installer-visible metadata from the checked-in
installer work (T11 manifest generator, T12 `install.sh`, release contract `RELEASING.md`)
must ship in the built sdist.

In scope: **package/source asset inclusion and lookup**, plus tests that prove it from
outside the checkout. Out of scope: T14 E2E lanes, T15 docs rewrite, T16 release-workflow
checks.

## 2. Current state (reuse targets)

Runtime behavior is already implemented by T02/T05; T13 is mainly completeness + verification:

- `pyproject.toml` `force-include` already relocates top-level assets into
  `awf/bootstrap_assets/…`; the sdist `include` already ships `docker/…`, `migrations`,
  `src`, `openapi.json`, etc. The sdist does **not** yet ship the installer metadata
  (`packaging/install.sh`, `scripts/generate_install_manifest.py`, `RELEASING.md`).
- `src/awf/service/bootstrap.py` — packaged-asset resolution is done
  (`_packaged_bootstrap_asset_root`, `is_packaged_bootstrap_asset_root`,
  `get_bootstrap_asset_root`, `_is_bootstrap_asset_root` requiring the five wheel markers).
- `src/awf/cli/start_commands.py` + `src/awf/cli/init_ops._resolve_service_compose_paths` —
  `awf start` selects verified source-checkout assets when given/recorded, else falls through
  to discovery that includes the packaged fallback; stale/invalid recorded source fails loudly.
- `src/awf/host_setup/source_assets.py` — `verified_source_from_metadata()` raises
  `SOURCE_CHECKOUT_ASSETS_STALE` and never consults packaged assets (no-mask by construction).

## 3. Requirements checklist (acceptance → verification)

- [ ] **AC1** Clean wheel install outside the checkout runs `--help` for `awf`, `setup`,
      `start`, `init`, `mcp serve` without repo-relative files. → build-backed clean-install
      smoke (§6.B).
- [ ] **AC2** `awf start` locates package assets when no source checkout is configured. →
      extracted-wheel packaged-root resolution test (§6.C) + start selection test (§6.E).
- [ ] **AC3** Source-checkout mode remains explicit and does not mask stale package assets. →
      no-mask invariant at `source_assets` and `awf start` levels (§6.D).
- [ ] **AC4** Installer/bootstrap metadata needed by the package lane ships in built
      artifacts. → sdist installer-metadata inclusion (§5.1) + wheel/sdist content & metadata
      tests (§6.A) + version-drift guard (§6.A).

## 4. Files / modules to touch

Production (minimal, additive only):
- `pyproject.toml` — add three installer-metadata entries to the **sdist** `include` list.

Tests (the bulk):
- `tests/packaging_build.py` *(new shared helper)* — build wheel+sdist once per worker.
- `tests/unit/cli/test_packaging.py` — extend static asserts for the new sdist includes.
- `tests/unit/cli/test_package_build_contents.py` *(new, `slow`)* — build-backed wheel/sdist
  content + distribution metadata + version-drift.
- `tests/unit/cli/test_clean_install_smoke.py` *(new, `slow`)* — clean-install help smoke from
  outside the checkout.
- `tests/unit/service/test_bootstrap_packaged_assets.py` — extend with extracted-wheel
  packaged-root resolution exercised from outside the checkout.
- `tests/unit/service/test_host_setup_source_assets.py` — extend with "stale not masked by an
  available packaged root".
- `tests/unit/cli/test_start_commands.py` — extend with start-level selection/no-mask test.

No new `src/awf/host_setup` production module (AGENTS.md prefers existing patterns; the
source/package selection contract already exists and is testable from outside the checkout).
This also avoids widening the `host_setup/__init__.py` coordination overlap.

## 5. Implementation steps (smallest green change after each failing test)

### 5.1 Packaging: installer metadata into the sdist
Add to `[tool.hatch.build.targets.sdist].include`: `/packaging/install.sh`,
`/scripts/generate_install_manifest.py`, `/RELEASING.md`. Purely additive; does not alter the
wheel payload (packaged bootstrap root is already valid; the curl installer is fetched
out-of-band, not needed at wheel runtime).

### 5.2 No runtime/source code changes expected
The packaged-asset lookup, `awf start` selection, and the no-mask invariant are already
implemented. If a build-backed test reveals a concrete gap, fix it as the smallest
`pyproject.toml` change and capture a regression test; record any such change in the
validation doc.

## 6. Tests to write first (TDD)

Build approach: the dev `[extra]` does not install `hatchling`, so the in-process
`hatchling.build` hooks are unavailable. Tests build via the `uv build` PEP 517 frontend
(the repo's standard build tool) into a process-lifetime temp dir, cached once per worker,
and **skip** with a clear reason when `uv` or the offline build is unavailable (the full lane
is owned by T14). Marked `slow`, with a raised per-test timeout.

- **A. Content (`test_package_build_contents.py`)** — wheel zip presence of every packaged
  bootstrap asset + the five `_is_bootstrap_asset_root` markers + every
  `control-plane.Dockerfile` COPY input; `docker/compose/.env` absent; `*.dist-info/METADATA`
  Name/Version; `awf` console entry point; sdist top-level assets **plus** new installer
  metadata; `docker/compose/.env` absent from sdist; version-drift guard
  (`awf.__version__ == pyproject[project].version`, and the installed
  `agent-workspace-fabric` metadata version when resolvable).
- **B. Clean-install help smoke (`test_clean_install_smoke.py`)** — from an extracted wheel
  with an explicit import path (wheel first, repo `src` removed) and cwd **outside** the
  checkout, run `awf`, `setup`, `start`, `init`, `mcp serve` `--help` as a subprocess; assert
  exit 0 + a stable fragment and that `awf` imports from the wheel, not repo `src`. Also a
  best-effort full `python -m venv` + install lane that skips when deps cannot be installed
  offline.
- **C. Packaged-root resolution from an extracted wheel** (extend
  `test_bootstrap_packaged_assets.py`) — extract `awf/bootstrap_assets/`, monkeypatch
  `bootstrap.files` to a fake whose `joinpath("bootstrap_assets")` → the extracted `Path`,
  cwd outside any checkout; assert the packaged root resolves and passes
  `_is_bootstrap_asset_root`, and `_resolve_bootstrap_assets(...)` returns compose/agent-runtime
  paths under the extracted root with `compose_env_file is None` and
  `_bootstrap_environment_file(...) == Path(".env")`.
- **D. Stale source not masked by packaged assets** — `test_host_setup_source_assets.py`: with
  a valid packaged root available, record valid source metadata, invalidate the checkout, and
  assert `verified_source_from_metadata(...)` raises `SOURCE_CHECKOUT_ASSETS_STALE` and offers
  no packaged fallback. `test_start_commands.py`: recorded (now-stale) source metadata makes
  `_resolve_start_source_checkout(None)` raise instead of falling back to packaged discovery.
- **E. Start uses packaged assets when no source checkout configured** (extend
  `test_start_commands.py`) — no `source_checkout` → `_resolve_start_source_checkout(None) is
  None` and `_resolve_start_bootstrap_inputs(None)` sources compose paths from
  `get_bootstrap_asset_root()` (stubbed packaged root via `init_ops`).

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

Per the AWF workspace contract, the full `.awf/workspace.yml` suite, whole-repo coverage gate,
and console build are not run in the agent phase — AWF/GitHub CI own them after completion.

## 8. Risks, assumptions, non-goals

- **Build availability.** `uv build` requires `uv` and a resolvable build backend; tests skip
  with a clear reason when unavailable. CI (which builds release artifacts) runs them fully.
- **Editable-install shadowing.** The smoke test removes the repo `src` path and prepends the
  extracted wheel dir, then asserts `awf.__file__` resolves under the wheel before asserting
  help output.
- **Coordination overlap (`STALE_OVERLAP`).** `pyproject.toml` edits stay purely additive
  (three sdist `include` lines); do not edit `host_setup/__init__.py`, `host_setup/config.py`,
  or `test_host_setup_config.py` (owned by active workspaces).
- **No new abstraction.** A `host_setup/package_assets.py` selection façade is deferred; the
  contract already exists. `service/worker.py`/`readiness.py` repo-relative resolution and the
  demo path are container/E2E concerns flagged for T10/T14, not changed here.
